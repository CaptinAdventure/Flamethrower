import hashlib
import json
import logging
import math
import threading

from django.db import close_old_connections
from django.db.models import F

from .models import DismissedSuggestion, Task, TaskLink, WorkType

logger = logging.getLogger(__name__)
MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"


def settings_instance():
    from .models import SimilaritySettings

    return SimilaritySettings.objects.get_or_create(pk=1)[0]


def valid_embedding(vector):
    return (
        isinstance(vector, list) and len(vector) == 384
        and all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector)
        and any(vector)
    )


def work_type_snapshot():
    types = list(WorkType.objects.order_by("pk").values("id", "title", "description"))
    data = {"types": types, "model": MODEL_REVISION, "max_tokens": settings_instance().max_tokens}
    signature = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return types, signature


def work_type_suggestion(task):
    if task.relates_indexing == Task.IndexingStatus.FAILED:
        return {"status": "index_failed"}
    if task.relates_indexing != Task.IndexingStatus.DONE:
        return {"status": "indexing"}
    _, signature = work_type_snapshot()
    if task.work_type_suggestion_signature != signature:
        return {"status": "pending"}
    if task.work_type_suggestion_error:
        return {"status": "failed", "error": task.work_type_suggestion_error}
    if task.suggested_work_type_id:
        return {"status": "ready", "work_type": task.suggested_work_type}
    return {"status": "failed", "error": "No work types are available."}


def process_work_type_suggestion(model_provider, cache):
    types, signature = work_type_snapshot()
    task = Task.objects.filter(
        relates_indexing=Task.IndexingStatus.DONE,
    ).exclude(work_type_suggestion_signature=signature).order_by("pk").first()
    if task is None:
        return False
    revision = task.indexing_revision
    try:
        if not types:
            raise ValueError("No work types are available.")
        if not valid_embedding(task.embedding):
            raise ValueError("The task embedding is invalid. Retry the suggestion to reindex the task.")
        if cache.get("signature") != signature:
            model = model_provider()
            model.max_seq_length = settings_instance().max_tokens
            vectors = model.encode(
                [f"{item['title']}\n\n{item['description']}" for item in types],
                normalize_embeddings=True,
            )
            vectors = [[float(value) for value in vector] for vector in vectors]
            if len(vectors) != len(types) or any(not valid_embedding(vector) for vector in vectors):
                raise ValueError("The embedding model returned invalid work-type vectors.")
            cache.update(signature=signature, vectors=vectors)
        scores = [sum(a * b for a, b in zip(task.embedding, vector)) for vector in cache["vectors"]]
        selected = types[max(range(len(types)), key=lambda index: scores[index])]["id"]
        error = ""
    except Exception as exc:
        logger.exception("Work-type suggestion failed for task %s", task.pk)
        selected = None
        error = f"{type(exc).__name__}: {exc}"[:2000]
    if work_type_snapshot()[1] != signature:
        return True
    Task.objects.filter(
        pk=task.pk, indexing_revision=revision, relates_indexing=Task.IndexingStatus.DONE,
    ).exclude(work_type_suggestion_signature=signature).update(
        suggested_work_type_id=selected, work_type_suggestion_signature=signature,
        work_type_suggestion_error=error,
    )
    return True


def suggestions(task, config):
    if task.relates_indexing != Task.IndexingStatus.DONE or not task.embedding:
        return []
    linked_ids = set(TaskLink.objects.filter(source=task).values_list("target_id", flat=True))
    linked_ids.update(TaskLink.objects.filter(target=task).values_list("source_id", flat=True))
    dismissed = {
        item.candidate_id: item.candidate_revision
        for item in DismissedSuggestion.objects.filter(task=task, task_revision=task.indexing_revision)
    }
    results = []
    for candidate in Task.objects.filter(relates_indexing=Task.IndexingStatus.DONE).select_related("work_type").exclude(pk=task.pk):
        if candidate.pk in linked_ids or dismissed.get(candidate.pk) == candidate.indexing_revision:
            continue
        if not candidate.embedding or len(candidate.embedding) != len(task.embedding):
            continue
        score = sum(a * b for a, b in zip(task.embedding, candidate.embedding))
        if math.isfinite(score) and score >= config.threshold:
            results.append({"task": candidate, "score": score})
    return sorted(results, key=lambda item: (-item["score"], item["task"].pk))[:config.max_results]


def process_one(model_provider):
    task = Task.objects.filter(relates_indexing=Task.IndexingStatus.PENDING).order_by("pk").first()
    if task is None:
        return False
    revision = task.indexing_revision
    claimed = Task.objects.filter(
        pk=task.pk, relates_indexing=Task.IndexingStatus.PENDING, indexing_revision=revision,
    ).update(relates_indexing=Task.IndexingStatus.IN_PROGRESS, indexing_error="")
    if not claimed:
        return True
    try:
        config = settings_instance()
        model = model_provider()
        model.max_seq_length = config.max_tokens
        vector = model.encode(f"{task.title}\n\n{task.request}", normalize_embeddings=True)
        embedding = [float(value) for value in vector]
        if len(embedding) != 384 or not all(math.isfinite(value) for value in embedding):
            raise ValueError("The embedding model returned an invalid vector.")
        if not any(embedding):
            raise ValueError("The embedding model returned a zero vector.")
    except Exception as error:
        logger.exception("Similarity indexing failed for task %s", task.pk)
        Task.objects.filter(pk=task.pk, indexing_revision=revision, relates_indexing=Task.IndexingStatus.IN_PROGRESS).update(
            relates_indexing=Task.IndexingStatus.FAILED,
            indexing_error=f"{type(error).__name__}: {error}"[:2000], embedding=None,
        )
    else:
        Task.objects.filter(pk=task.pk, indexing_revision=revision, relates_indexing=Task.IndexingStatus.IN_PROGRESS).update(
            relates_indexing=Task.IndexingStatus.DONE, indexing_error="", embedding=embedding,
            work_type_suggestion_signature="", suggested_work_type=None, work_type_suggestion_error="",
        )
    return True


def recover_interrupted():
    Task.objects.filter(relates_indexing=Task.IndexingStatus.IN_PROGRESS).update(
        relates_indexing=Task.IndexingStatus.PENDING, indexing_revision=F("indexing_revision") + 1,
        indexing_error="", embedding=None,
    )


def start_worker():
    stop = threading.Event()

    def run():
        model = None
        needs_recovery = False
        work_type_cache = {}

        def get_model():
            nonlocal model
            if model is None:
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(MODEL_NAME, revision=MODEL_REVISION, trust_remote_code=False)
            return model

        while not stop.is_set():
            try:
                close_old_connections()
                if needs_recovery:
                    recover_interrupted()
                    needs_recovery = False
                if not process_one(get_model) and not process_work_type_suggestion(get_model, work_type_cache):
                    stop.wait(3)
            except Exception:
                logger.exception("Similarity worker failed; retrying.")
                needs_recovery = True
                stop.wait(3)
            finally:
                close_old_connections()

    thread = threading.Thread(target=run, name="similarity-indexer", daemon=True)
    thread.start()
    return stop, thread

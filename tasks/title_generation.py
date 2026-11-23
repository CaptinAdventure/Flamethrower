import re
from functools import lru_cache

MODEL_NAME = "google/flan-t5-small"
MODEL_REVISION = "0fc9ddf78a1e988dac52e2dac162b0ede4fd74ab"


class UnusableTitleError(ValueError):
    pass


def has_repetition(title):
    words = re.findall(r"\w+", title.casefold())
    return any(
        words[index:index + size] == words[index + size:index + 2 * size]
        for size in range(1, 4)
        for index in range(len(words) - 2 * size + 1)
    )


@lru_cache(maxsize=1)
def load_model():
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, revision=MODEL_REVISION, trust_remote_code=False)
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_NAME, revision=MODEL_REVISION, trust_remote_code=False)
    model.eval()
    return tokenizer, model


def generate_title(request_text):
    import torch

    tokenizer, model = load_model()
    prompt = f"Write a concise title: {request_text}"
    inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=512)
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=40, do_sample=False)
    title = tokenizer.decode(output[0], skip_special_tokens=True).strip()
    if not title:
        raise UnusableTitleError("The local model returned an empty title.")
    title = title.splitlines()[0][:200].strip()
    if not re.search(r"\w", title) or has_repetition(title):
        raise UnusableTitleError("The local model returned an unusable title.")
    return title

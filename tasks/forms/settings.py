from pathlib import Path

from django import forms

from ..models import SimilaritySettings


class SimilaritySettingsForm(forms.ModelForm):
    threshold = forms.FloatField(min_value=-1, max_value=1, help_text="Minimum cosine similarity (-1 to 1).")
    max_results = forms.IntegerField(min_value=1, max_value=100, help_text="Maximum suggestions per task.")
    max_tokens = forms.IntegerField(min_value=8, max_value=512, help_text="Maximum model tokens per task (including special tokens). Changing this reindexes all tasks.")

    class Meta:
        model = SimilaritySettings
        fields = ["threshold", "max_results", "max_tokens"]


class SafeReadPathsForm(forms.Form):
    safe_read_paths = forms.CharField(
        required=False, label="Additional auto-approved read/search paths",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="One absolute, existing file or directory per line. Directories include descendants. "
                  "Symlinks are saved as their resolved targets. Clear the list to remove all additional paths.",
    )

    def clean_safe_read_paths(self):
        entries = []
        for line in self.cleaned_data["safe_read_paths"].splitlines():
            value = line.strip()
            if not value:
                continue
            path = Path(value)
            if not path.is_absolute():
                raise forms.ValidationError("Use an absolute path: %(path)s", params={"path": value})
            try:
                resolved = path.resolve(strict=True)
                if resolved.is_dir():
                    kind = "directory"
                elif resolved.is_file():
                    kind = "file"
                else:
                    raise forms.ValidationError("Not a regular file or directory: %(path)s", params={"path": value})
            except (OSError, RuntimeError, ValueError) as error:
                raise forms.ValidationError(
                    "Cannot access path %(path)s: %(error)s", params={"path": value, "error": str(error)},
                ) from error
            entry = {"path": str(resolved), "kind": kind}
            if entry not in entries:
                entries.append(entry)
        return entries

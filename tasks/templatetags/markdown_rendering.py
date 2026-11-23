from django import template
from django.utils.safestring import mark_safe
from markdown_it import MarkdownIt

register = template.Library()
# Agent text is untrusted; do not execute HTML or load remote images.
_markdown = MarkdownIt("commonmark", {"html": False}).disable("image")


@register.filter
def render_markdown(value):
    return mark_safe(_markdown.render(value or ""))

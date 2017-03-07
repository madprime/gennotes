import bleach
import markdown as markdown_library

from django.template.defaulttags import register
from django.utils.safestring import mark_safe


@register.filter
def markdown(value):
    """
    Translate markdown to a safe subset of HTML.
    """
    cleaned = bleach.clean(markdown_library.markdown(value),
                           tags=bleach.ALLOWED_TAGS +
                           ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])

    linkified = bleach.linkify(cleaned)

    return mark_safe(linkified)

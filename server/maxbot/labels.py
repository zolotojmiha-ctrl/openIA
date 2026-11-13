"""Display one location when object and section carry the same name."""
import re


def location(object_name,section_name,separator=' · '):
    clean=lambda value:re.sub(r'\s+',' ',str(value or '')).strip()
    normalize=lambda value:re.sub(r'\s*[-–—]\s*','-',clean(value)).casefold()
    first=clean(object_name);second=clean(section_name)
    return first if not second or normalize(first)==normalize(second) else separator.join(p for p in (first,second) if p)

import re


def looks_like_tin(value: str) -> bool:
    """Heuristic: Ethiopian clinic TINs are mostly digits (8+)."""
    text = (value or "").strip()
    if not text:
        return False
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8:
        return False
    return len(digits) >= max(len(text) - 2, 8)


def normalize_clinic_identity(tin: str, name: str) -> tuple[str, str]:
    """
    Correct clinic_tin / clinic_name when values were stored swapped
    (e.g. name in TIN field and numeric TIN in name field).
    """
    tin_value = (tin or "").strip()
    name_value = (name or "").strip()
    tin_like = looks_like_tin(tin_value)
    name_like = looks_like_tin(name_value)

    if not tin_like and name_like:
        return name_value, tin_value
    return tin_value, name_value

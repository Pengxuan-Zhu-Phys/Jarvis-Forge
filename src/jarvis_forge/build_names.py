"""User-facing app names for both current and historical build IDs."""


def app_name(build_id):
    return build_id.lower().removesuffix("-build") or build_id.lower()

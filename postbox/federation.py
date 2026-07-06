def parse_address(to: str) -> tuple[str, str | None]:
    """Return (name, domain) for an address, treating empty domains as local."""
    address = to.strip()
    if "@" not in address:
        return address, None

    name, domain = address.split("@", 1)
    domain = domain.strip()
    return name.strip(), domain or None

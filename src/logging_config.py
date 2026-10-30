import logging


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


def mask_phone(number: str) -> str:
    """Mask a phone number for logging, e.g. +265991234567 -> +265******567."""
    if len(number) <= 7:
        return "*" * len(number)
    return f"{number[:4]}{'*' * (len(number) - 7)}{number[-3:]}"

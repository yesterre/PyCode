from dotenv import load_dotenv


def load_environment() -> None:
    """Load local development settings without overriding process variables."""
    load_dotenv(override=False)

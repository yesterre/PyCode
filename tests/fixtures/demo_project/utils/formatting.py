import re


def format_user_name(name: str) -> str:
    return name.strip().title()


def normalize_email(email: str) -> str:
    return email.strip().lower()


class NameFormatter:
    def format(self, name: str) -> str:
        cleaned = re.sub(r"\s+", " ", name)
        return format_user_name(cleaned)

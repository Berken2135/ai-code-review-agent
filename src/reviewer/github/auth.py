"""Personal Access Token authentication. GitHub App auth is future work."""

from pydantic import SecretStr


class GitHubAuthError(RuntimeError):
    pass


def auth_headers(token: SecretStr | str | None) -> dict[str, str]:
    value = token.get_secret_value() if isinstance(token, SecretStr) else token
    if not value:
        raise GitHubAuthError("GITHUB_TOKEN is not configured")
    return {"Authorization": f"Bearer {value}"}

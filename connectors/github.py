"""GitHub reference connector — read repo metadata plus a low-risk write
(star/unstar). Runs against MockTransport offline; the adapter is written
against the Transport interface so a real HTTPS transport slots in without
changing operation logic or policy mappings."""
from __future__ import annotations

from connectors.models import ConnectorManifest, OperationDef
from connectors.rest import RESTConnector


def _obj(**props):
    return {"type": "object", "properties": props, "additionalProperties": False}


GITHUB_MANIFEST = ConnectorManifest(
    name="github",
    version="1.0.0",
    display_name="GitHub",
    auth_kinds=["oauth_pkce", "api_key"],
    scopes=["repo.read", "repo.write"],
    operations=[
        OperationDef(
            name="get_repo",
            description="Read public repository metadata (name, description, stars).",
            risk="R0", side_effect="none", scopes_required=["repo.read"],
            input_schema=_obj(
                owner={"type": "string"}, repo={"type": "string"}),
            output_schema=_obj(),
            idempotency="pure",
        ),
        OperationDef(
            name="star",
            description="Star a repository on the connected account. Low-risk "
                        "external write; requires a bound approval.",
            risk="R3", side_effect="external_write", scopes_required=["repo.write"],
            input_schema=_obj(
                owner={"type": "string"}, repo={"type": "string"}),
            output_schema=_obj(),
            idempotency="keyed",
        ),
        OperationDef(
            name="unstar",
            description="Remove a star from a repository on the connected account.",
            risk="R3", side_effect="external_write", scopes_required=["repo.write"],
            input_schema=_obj(
                owner={"type": "string"}, repo={"type": "string"}),
            output_schema=_obj(),
            idempotency="keyed",
        ),
    ],
)


class GitHubConnector(RESTConnector):
    manifest = GITHUB_MANIFEST
    base_url = "https://api.github.com"

    def auth_headers(self, credential: str) -> dict:
        return {"Authorization": f"Bearer {credential}",
                "Accept": "application/vnd.github+json"}

    def execute(self, op_name: str, credential: str, args: dict) -> dict:
        owner = args["owner"]
        repo = args["repo"]
        path = f"/repos/{owner}/{repo}"
        if op_name == "get_repo":
            return self._call(credential=credential, method="GET", path=path)
        if op_name == "star":
            self._call(credential=credential, method="PUT",
                       path=f"/user/starred{path}")
            return {"starred": True, "owner": owner, "repo": repo}
        if op_name == "unstar":
            self._call(credential=credential, method="DELETE",
                       path=f"/user/starred{path}")
            return {"starred": False, "owner": owner, "repo": repo}
        # Undeclared operations never reach here — the registry enforces the
        # manifest first — but defense in depth costs one line.
        from connectors.models import ConnectorError
        raise ConnectorError("UNKNOWN_OPERATION",
                             f"github declares no operation {op_name!r}.")


def mock_github_transport() -> "MockTransport":
    """Canned GitHub API for offline demos and tests."""
    from connectors.rest import MockTransport, HTTPResponse

    t = MockTransport()
    t.add_route("GET", "/repos/0sparsh2/OpenMuse",
                lambda p, b, h: HTTPResponse(200, {
                    "full_name": "0sparsh2/OpenMuse",
                    "description": "Muse-like personal agent platform (replica)",
                    "stargazers_count": 3, "private": False,
                }))
    t.add_route("PUT", "/user/starred/repos/0sparsh2/OpenMuse",
                lambda p, b, h: HTTPResponse(204, {}))
    t.add_route("DELETE", "/user/starred/repos/0sparsh2/OpenMuse",
                lambda p, b, h: HTTPResponse(204, {}))
    return t

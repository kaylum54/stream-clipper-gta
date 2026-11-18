import json
import re
from pathlib import Path


from clipper.poster import build_post_body, default_platform_configurations

SPEC = json.loads((Path(__file__).parent / "fixtures/postforme/openapi-excerpt.json").read_text(encoding="utf-8"))
S = SPEC["schemas"]
TYPES = {"string": str, "boolean": bool, "number": (int, float), "array": list, "object": dict}


def resolve(schema):
    if "$ref" in schema:
        return S[schema["$ref"].rsplit("/", 1)[1]]
    if "allOf" in schema:
        return resolve(schema["allOf"][0])
    return schema


def check(value, schema, where="body"):
    """Tiny checker: required, unknown properties, types, enums. Returns a list of problems."""
    schema = resolve(schema)
    errs = []
    if value is None:
        return errs if schema.get("nullable") else [f"{where} is null"]
    t = schema.get("type")
    if t in TYPES and (not isinstance(value, TYPES[t]) or (t == "number" and isinstance(value, bool))):
        return [f"{where} should be {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}={value!r} not in {schema['enum']}")
    if t == "array" and "items" in schema:
        for i, v in enumerate(value):
            errs += check(v, schema["items"], f"{where}[{i}]")
    if t == "object" and "properties" in schema:
        props = schema["properties"]
        errs += [f"{where}.{k} unknown" for k in value if k not in props]
        errs += [f"{where}.{k} required" for k in schema.get("required", []) if k not in value]
        for k, v in value.items():
            if k in props:
                errs += check(v, props[k], f"{where}.{k}")
    return errs


def body():
    return build_post_body("cap", ["spc_1"], "https://cdn.test/a.mp4", default_platform_configurations("T" * 150), "ext1")


def test_post_body_matches_create_social_post_dto():
    # catches: a renamed/misspelled field or wrong type that the real API would 400
    assert check(body(), S["CreateSocialPostDto"]) == []


def test_checker_catches_bad_body():
    # catches: a checker that passes everything
    assert check({"caption": 1, "extra": 1}, S["CreateSocialPostDto"])


def test_platform_configurations_fields_and_enums_exist():
    # catches: sending a platform option the API doesn't have, or an enum value outside the spec
    cfg = default_platform_configurations("t")
    assert check(cfg, S["PlatformConfigurationsDto"].get("properties") and {"type": "object", "properties": S["PlatformConfigurationsDto"]["properties"]}, "cfg") == []


def test_youtube_title_truncated_to_100():
    # catches: YouTube rejecting titles over 100 chars
    assert len(default_platform_configurations("x" * 300)["youtube"]["title"]) == 100


def test_paths_called_by_poster_exist_in_spec():
    # catches: calling an endpoint/method the spec doesn't define
    src = (Path(__file__).parent.parent / "clipper/poster.py").read_text(encoding="utf-8")
    calls = set(re.findall(r'_req\(\s*"(GET|POST|PUT)",\s*f?"(/v1/[^"]+)"', src))
    assert calls
    unverified = []
    for method, path in calls:
        norm = re.sub(r"\{[^}]+\}", "{id}", path)
        spec_paths = {re.sub(r"\{[^}]+\}", "{id}", p): v for p, v in SPEC["paths"].items()}
        if norm not in spec_paths:
            unverified.append((method, path))
        else:
            assert method.lower() in spec_paths[norm], (method, path)
    # GET /v1/social-posts/{id} is not in the saved excerpt: known gap, see build notes
    assert unverified == [("GET", "/v1/social-posts/{post_id}")]


def test_query_params_used_exist_in_spec():
    # catches: filtering results/accounts by a param name the API ignores
    def names(p, m): return {x["name"] for x in SPEC["paths"][p][m]["parameters"]}
    assert "post_id" in names("/v1/social-post-results", "get")
    assert {"status", "limit"} <= names("/v1/social-accounts", "get")

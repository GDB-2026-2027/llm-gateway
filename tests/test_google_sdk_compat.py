"""Check adapter use of the installed google-genai SDK without network access."""

from google.genai import types

from llm_gateway.api_models import ResponsesRequest
from llm_gateway.config import GoogleConfig, ModelRoute
from llm_gateway.providers import call_google_genai, embed_google_genai


def test_google_genai_3_request_response_shapes(monkeypatch):
    seen = []

    class Models:
        def generate_content(self, *, model, contents, config):
            seen.append(("response", model, contents, config))
            return types.GenerateContentResponse(
                candidates=[types.Candidate(content=types.Content(
                    role="model", parts=[types.Part(text='{"answer":true}')]
                ))],
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=5, candidates_token_count=2, thoughts_token_count=3,
                ),
            )

        def embed_content(self, *, model, contents, config):
            seen.append(("embedding", model, contents, config))
            return types.EmbedContentResponse(embeddings=[types.ContentEmbedding(
                values=[0.1, 0.2],
                statistics=types.ContentEmbeddingStatistics(token_count=4),
            )])

    class Client:
        def __init__(self, **kwargs):
            seen.append(("client", kwargs))
            self.models = Models()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def close(self):
            pass

    monkeypatch.setattr("llm_gateway.providers.genai.Client", Client)
    payload = ResponsesRequest.model_validate({
        "model": "smoke/google", "instructions": "Return JSON",
        "input": [{"role": "user", "content": "Test"}],
        "text": {"format": {"type": "json_schema", "name": "answer", "strict": True,
                            "schema": {"type": "object", "properties": {"answer": {"type": "boolean"}},
                                       "required": ["answer"]}}},
        "metadata": {"run_id": "static", "cache_namespace": "static"},
    })
    google = GoogleConfig(project_id="fake-project", location="global")
    route = ModelRoute(backend="google_genai", upstream_model="fake-model", json_schema_mode="native")
    response = call_google_genai(payload, route, google)
    assert response.text == '{"answer":true}'
    assert (response.tokens_in, response.tokens_out) == (5, 5)
    assert seen[0] == ("client", {"vertexai": True, "project": "fake-project", "location": "global"})
    assert seen[1][3].response_json_schema == payload.text.format.schema_definition

    embedding_route = ModelRoute(
        backend="google_genai", upstream_model="fake-embedding", task="embeddings",
        embedding_dimensions=2, location="europe-west4",
    )
    embedding = embed_google_genai(["Test"], embedding_route, google)
    assert embedding.vectors == [[0.1, 0.2]]
    assert embedding.tokens_in == 4
    assert seen[2][1]["location"] == "europe-west4"
    assert seen[3][3].output_dimensionality == 2

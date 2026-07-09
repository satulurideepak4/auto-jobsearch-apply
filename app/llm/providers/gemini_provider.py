from __future__ import annotations

import os

from app.llm.base import LLMProvider


class GeminiProvider(LLMProvider):
    """LLM provider backed by Google Gemini via Vertex AI.

    Authenticates via service account JSON key — no gcloud CLI required.
    Key file path is read from GOOGLE_APPLICATION_CREDENTIALS env var
    (set in .env and loaded by load_dotenv() at app startup).

    The SDK client is created once at instantiation and reused across calls.
    AFC (Automatic Function Calling) is disabled — we never pass tools.
    """

    def __init__(self, project_id: str, location: str, model: str) -> None:
        if not project_id:
            raise ValueError(
                "GCP_PROJECT_ID is not set. Add it to your .env file.\n"
                "Find it at: https://console.cloud.google.com → project selector (top bar)."
            )
        self.project_id = project_id
        self.location = location
        self.model = model
        self._client = self._build_client()

    def _build_client(self):
        from google import genai
        from google.oauth2 import service_account
        import google.auth
        import logging

        logger = logging.getLogger(__name__)
        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./vertex-ai-credentials.json")
        credentials = None

        if os.path.exists(cred_path):
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    cred_path,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                logger.info("Loaded Vertex AI credentials from %s", cred_path)
            except Exception as exc:
                logger.warning("Failed to load credentials from %s: %s", cred_path, exc)
        else:
            logger.warning(
                "Vertex AI key file not found at %s. "
                "Attempting Application Default Credentials (ADC)...",
                cred_path
            )
            try:
                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                logger.info("Loaded Application Default Credentials (ADC) successfully.")
            except Exception as exc:
                logger.warning("Failed to load Application Default Credentials: %s", exc)

        # Fallback to GEMINI_API_KEY if no credentials and an API key is available
        api_key = os.environ.get("GEMINI_API_KEY")
        if credentials is None and api_key:
            logger.info("Using GEMINI_API_KEY for standard non-Vertex Gemini Client.")
            return genai.Client(api_key=api_key)

        return genai.Client(
            vertexai=True,
            project=self.project_id,
            location=self.location,
            credentials=credentials,
        )

    def generate(self, prompt: str, **params) -> str:
        from google.genai import types
        from app.config import get_settings
        s = get_settings()

        config = types.GenerateContentConfig(
            temperature=params.get("temperature", s.LLM_TEMPERATURE),
            max_output_tokens=params.get("max_tokens", s.LLM_MAX_TOKENS),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
        return response.text

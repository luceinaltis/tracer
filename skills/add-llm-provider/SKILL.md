---
name: add-llm-provider
description: >
  Guide for implementing a new LLM provider. Use when adding support for a new
  LLM (e.g., OpenAI, Gemini, local models) to the Tracer system. Ensures the
  provider follows the abstraction interface and handles prompt differences.
---

# Add LLM Provider

Step-by-step guide for adding a new LLM to Tracer.

## Before You Start

- Read `docs/architecture.md` for LLM abstraction design.
- Read `src/tracer/llm/base.py` for the LLMProvider interface (once implemented).
- Understand the role system: researcher, analyst, strategist, reporter.

## Key Design Decisions

### Prompt Adaptation
Different models respond differently to the same prompt. When adding a provider:
- Test with existing prompts first.
- If quality is poor, create model-specific prompt templates.
- Store in `src/tracer/llm/prompts/{provider_name}/` if needed.

### Structured Output
Tracer agents expect structured responses (JSON, typed objects). Ensure:
- The provider supports structured output (function calling, JSON mode, etc.)
- Fallback: parse unstructured text if structured mode unavailable.

## Checklist

1. **Research the API**
   - Auth method, pricing, rate limits, context window size
   - Structured output support (function calling, JSON mode)
   - Streaming support (optional but recommended)

2. **Create provider file**
   - Path: `src/tracer/llm/{provider_name}.py`
   - Implement `LLMProvider` interface from `base.py`
   - All calls must be `async`
   - Handle rate limiting and retries
   - Support both streaming and non-streaming

3. **Configuration**
   - API key via environment variable: `{PROVIDER_NAME}_API_KEY`
   - Model selection via config (e.g., `claude-sonnet-4-20250514`, `gpt-4o`)
   - Add to `.env.example`
   - Register in provider config/registry

4. **Write tests**
   - Path: `tests/llm/test_{provider_name}.py`
   - Mock API responses
   - Test: completion, structured output, error handling, rate limiting

5. **Update docs**
   - Add provider to `docs/architecture.md` LLM section
   - Document model-specific quirks or limitations

## Implementation Template

```python
from tracer.llm.base import LLMProvider, LLMResponse, Message


class MyNewLLM(LLMProvider):
    """LLM provider for MyNewAPI."""

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def complete(self, messages: list[Message]) -> LLMResponse:
        ...

    async def complete_structured(
        self, messages: list[Message], schema: dict
    ) -> dict:
        ...
```

## Validation

- [ ] Implements LLMProvider interface completely
- [ ] All methods are async
- [ ] Structured output works for agent roles
- [ ] Rate limiting handled
- [ ] Tests pass: `pytest tests/llm/test_{provider_name}.py`
- [ ] Type check passes: `pyright src/tracer/llm/{provider_name}.py`
- [ ] `docs/architecture.md` updated

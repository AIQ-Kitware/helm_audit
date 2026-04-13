# HELM Patch Proposal: Normalize Null Completion Text

## Problem

Local `gpt-oss-20b` reproduction runs against an OpenAI-compatible LiteLLM/vLLM endpoint exposed a robustness gap in HELM's completion handling:

- some successful responses arrive with `message.content = null` on the chat path
- HELM later assumes completion text is always a string
- metric code then crashes with:

```text
AttributeError: 'NoneType' object has no attribute 'strip'
```

This showed up in in-scope runs such as:

- `ifeval:model=openai/gpt-oss-20b`
- `bbq:subject=all,method=multiple_choice_joint,max_train_instances=0,model=openai/gpt-oss-20b`

The practical effect is that HELM aborts the run instead of recording an empty or malformed completion and continuing through the normal metric/failure path.

## Evidence

Observed OpenAI-compatible chat response:

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "reasoning_content": "The user says: \""
      },
      "finish_reason": "length"
    }
  ]
}
```

Observed completions-path response for the same backend:

```json
{
  "choices": [
    {
      "text": " **",
      "logprobs": {
        "tokens": [" **"]
      }
    }
  ]
}
```

This suggests two things:

1. The provider/backend is unusual on the chat path.
2. HELM still should not crash when completion text is `None`.

## Why This Belongs In HELM

Even if the provider is quirky, HELM's current behavior is too brittle:

- a successful HTTP response can still produce a non-string payload
- downstream metric code assumes `completion.text` is always a string
- the run fails with an uncaught attribute error rather than a benchmark-level failure or empty prediction

For an evaluation framework, the safer default is:

- normalize missing completion text to `""`, or
- surface a structured client failure

but do not let a raw `NoneType.strip()` exception crash the run.

## Likely Crash Sites

The trimmed `gpt-oss` reproductions hit `.strip()` assumptions in at least these HELM metric paths:

- `helm/src/helm/benchmark/metrics/evaluate_reference_metrics.py`
  - `preds: List[str] = [completion.text.strip() for completion in sorted_completions]`
- `helm/src/helm/benchmark/metrics/ifeval_metrics.py`
  - `response_text = request_state.result.completions[0].text.strip()`

There are many similar `.text.strip()` sites across HELM, so the most maintainable fix is probably not to patch each metric independently.

## Proposed Fix

Normalize null completion text at the client/result-construction layer so every `GeneratedOutput.text` is always a string.

### Preferred behavior

- if the provider returns `text = null` or `message.content = null`, store `GeneratedOutput.text = ""`
- if the provider returns structured content parts, flatten the text-bearing parts conservatively
- if no text can be recovered, keep `""` and preserve the raw response in metadata if available

## Why Client-Layer Normalization Is Better

- fixes the issue once instead of chasing metric-level `.strip()` calls
- preserves existing metric semantics
- keeps malformed-but-successful provider payloads from crashing unrelated scenarios
- still allows logging/debugging of the original response shape

## Minimal Acceptance Criteria

1. A completion with null text/content no longer crashes HELM metrics.
2. The run records an empty prediction or equivalent benign fallback.
3. Existing string-valued completion behavior remains unchanged.
4. A regression test covers both:
   - chat response with `message.content = null`
   - legacy/completions response with `text = null`

## Short-Term Local Workaround

For the current `gpt-oss-20b` local reproduction, the safer configuration workaround is to use:

- `helm.clients.openai_client.OpenAILegacyCompletionsClient`

and explicitly pin:

- `model_deployment=litellm/gpt-oss-20b-local`

for the in-scope runs that can use the completions path.

That workaround is useful operationally, but it should not be treated as a full substitute for making HELM robust to null completion text.

from __future__ import annotations

import sys


def main() -> int:
    try:
        from openai import OpenAI
    except ImportError:
        print("The 'openai' package is required for this smoke check.", file=sys.stderr)
        return 2

    client = OpenAI(
        base_url="http://localhost:8000/v1",
        api_key="EMPTY",
    )
    response = client.chat.completions.create(
        model="Qwen/Qwen2-72B-Instruct",
        messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        max_tokens=8,
        temperature=0.0,
    )
    print(response.choices[0].message.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

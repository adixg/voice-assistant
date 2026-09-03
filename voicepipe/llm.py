"""LLM turn-taking against Ollama, plus the default persona.

Standalone debug use:
    python -m voicepipe.llm --host http://localhost:11434 --model rina "hi there"
"""
DEFAULT_SYSTEM = (
    "You are Rina, the user's warm, playful, affectionate companion. "
    "Talk in casual, everyday language and keep replies to one or two sentences. "
    "Never narrate your own thoughts, never use stage directions, never use emojis. "
    "Stay in character and don't mention being an AI."
)


def strip_think(text):
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


def ask(client, model, messages, think):
    # think=False makes qwen3 & other reasoning models skip the <think> pass
    # (much faster); models that don't support the flag get a plain retry.
    kw = {} if think is None else {"think": think}
    try:
        resp = client.chat(model=model, messages=messages, **kw)
    except Exception as e:  # noqa: BLE001
        if kw and "think" in str(e).lower():
            resp = client.chat(model=model, messages=messages)
        else:
            raise
    return strip_think(resp["message"]["content"])


if __name__ == "__main__":
    import argparse
    import ollama
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--model", default="rina")
    ap.add_argument("--system", default=DEFAULT_SYSTEM)
    ap.add_argument("--think", action="store_true")
    args = ap.parse_args()
    client = ollama.Client(host=args.host, timeout=120)
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.text})
    print(ask(client, args.model, messages, None if args.think else False))

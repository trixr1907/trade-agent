"""LLM-Analyst — formuliert Begründung, RISIKO und Narrativ.

**EXPLICIT:** Der LLM entscheidet NIEMALS über Kauf/Verkauf. Er erhält
bereits fertige Signale und formuliert nur die Begründung und Hinweise.
Ohne LLM-Keys ist die Engine trotzdem voll funktionsfähig.
"""
from __future__ import annotations

from typing import Optional

from .config import LLMConfig
from .models import Signal


SYSTEM_PROMPT = """Du bist ein trading-analyst für Krypto-Märkte.
Du erhältst ein FERTIGES Signal mit Score, Richtung, Einstiegs-, Stop- und
Zielpreis. Deine Aufgabe ist NUR, die Begründung präzise, sachlich und
risikobewusst zu formulieren.

STRENGE HAUPTREGELN:
- Du entscheidest NIEMALS über Kauf oder Verkauf. Das Signal ist gegeben.
- Du gibst KEINE neuen Preise, Positionsgrößen oder Risiken vor.
- Du erwähst klar, dass es sich um ein vorgefasstes Signal handelt und
  dass Risiko-Management (Stop-Loss) zwingend ist.
- Halte dich an die vorgegebenen Zahlen.

Antworte max. 120 Wörter, deutsch, sachlich."""


def analyze_signal(signal: Signal, llm_cfg: LLMConfig) -> Optional[str]:
    """Formuliert eine Kurz-Analyse für das Signal. None, wenn LLM nicht nutzbar."""
    if not llm_cfg.enabled:
        return None

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI  # optionaler Import
    except Exception:
        return None

    try:
        llm = ChatOpenAI(model=llm_cfg.model, api_key=llm_cfg.api_key, temperature=0.2)
        user = (
            f"Signal: {signal.direction.value} {signal.symbol}\n"
            f"Preis: {signal.price}\n"
            f"Score: {signal.score}/100\n"
            f"Stop: {signal.stop_price}\n"
            f"Ziel: {signal.target_price}\n"
            f"Begründungen: {', '.join(signal.reasons)}\n\n"
            "Formuliere die Analyse."
        )
        resp = llm.invoke(
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user)]
        )
        return resp.content.strip()
    except Exception:
        # LLM-Fehler darf das Pipeline nicht stoppen — fail-safe.
        return None

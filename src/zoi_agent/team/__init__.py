"""Agno Team — Patricia (leader) + EstoqueExpert (member).

Substitui o `Responder` LLM e o dispatch heurístico de busca de estoque.

Pipeline por turno:
  Updater (parse_structured) -> StateUpdate
    -> Question Planner (determinístico)
      -> Team(coordinate, leader=Patricia, members=[EstoqueExpert])
        -> bolhas finais [abertura?, cards?, fechamento]
          -> send sob shield
"""
from __future__ import annotations

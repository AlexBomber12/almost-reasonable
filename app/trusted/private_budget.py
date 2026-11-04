"""Private-only budget admission; exhaustion never latches provider suspension."""

import sqlite3

from app.core.public_rejection import PublicOutputRejected
from app.trusted.openai_transport import OpenAITransportError
from app.trusted.public_monthly_budget import BudgetedResponses


def available(budget):
    """Read existing durable state; absence or corruption must not grant credit."""
    try:
        state = budget.snapshot()
        return (
            budget.lane == "private_sol"
            and state["charged_microusd"] + budget.policy[2] <= state["limit_microusd"]
        )
    except (OSError, ValueError, sqlite3.Error):
        return False


class PrivateBudgetedResponses(BudgetedResponses):
    """Use the atomic final SDK fence for every private capability and reply."""

    def create(self, **kwargs):
        try:
            return super().create(**kwargs)
        except PublicOutputRejected as error:
            raise OpenAITransportError(
                error_code=(
                    "private_monthly_budget_exhausted"
                    if error.code == "public_monthly_budget_exhausted"
                    else "private_budget_contract_invalid"
                )
            ) from None

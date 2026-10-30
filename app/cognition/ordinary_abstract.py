"""Bounded generality screen; unknown wording stays private for review.

This lexical H screen is not an anonymity proof. It admits combinations, not a
catalog of seeded substantive views. It excludes identifying and personal
domains conservatively; richer wording retains the existing exact-consent path.
"""

import re

from .contracts import PrivacyScreenDecision, PrivacyScreenResult
from .sharing import _normalized_words, screen_abstract_candidate


ORDINARY_POLICY_VERSION = "ordinary-abstract-to-shared-v1"
ORDINARY_SCREEN_VERSION = "ordinary-abstract-generality-v1"
_WORDS = frozenset(
    """a an the and or but not of in on to for from with without through before
    after between against under over by as at is are be can may might should
    compare consider investigate explore examine question doubt revise retain
    research understand understanding explain explanation explanations evidence
    counterevidence competing independent alternative alternatives uncertainty
    uncertain confidence knowledge belief beliefs hypothesis hypotheses theory
    theories model models causal causality correlation patterns pattern systems
    system complex complexity simple simplicity general abstract abstraction
    philosophical philosophy scientific science intellectual reasoning reason
    reasons rational rationality logic logical empirical conceptual concept
    concepts clarity coherence consistent consistency contradiction contradictions
    conflicting conflict pluralism skepticism fallibility provisional revisable
    subjective objective opinion opinions view views perspective perspectives
    adopting adopt settling long term direction inquiry curiosity learning learn
    truth meaning ethics ethical epistemic epistemology value values judgment
    judgments decision decisions review verification verified unverified test
    tests testing falsification prediction predictions limits limitations bounds
    structure structures process processes emergent emergence probability
    probabilities probabilistic information interpretation interpretations
    observation observations argument arguments assumptions assumption balance
    distinguish support supporting supports reduce increase strengthen weaken
    seek open unresolved resolved abandoned methodology methods method precision
    accuracy relevance robust robustness reflect reflection autonomy responsibility
    freedom free will consciousness identity continuity change changes changing
    stability time existence reality language formal informal mathematics
    mathematical art aesthetics ontology ontological metaphysics history historical
    possibility possibilities necessary sufficient contingent contingency
    comparing considering investigating exploring revising retaining questioning
    mechanisms mechanism this that these those some all any more less both
    different same better their its it how why what whether when which than if
    then while also
    и или но не в на для из с без до после между против при как о об
    сравнить рассмотреть исследовать изучать пересмотреть сохранить проверить
    сравнивать рассматривать исследование исследования вопрос вопросы сомнение
    сомнения неопределенность неопределённость объяснение объяснения доказательства
    свидетельства контраргументы гипотеза гипотезы теория теории модель модели
    причинность причинные причинных корреляция системы систем сложность сложных
    сложные простота общие общих абстрактные абстрактных философия философские
    философских научные научных наука познание знание знания убеждение убеждения
    мнение мнения уверенность альтернативы независимые независимых конкурирующие
    конкурирующих обоснование обоснования логика логические эмпирические понятия
    ясность согласованность противоречия истина смысл этика ценности суждение
    суждения решение решения пересмотр проверка проверенные непроверенные
    проверяемость предсказание предсказания ограничения структура структуры
    процесс процессы вероятность вероятности информация интерпретация
    интерпретации наблюдение наблюдения аргумент аргументы предположения баланс
    различать поддерживать снижать повышать искать метод методология методы
    точность устойчивость автономия ответственность свобода сознание идентичность
    непрерывность изменение изменения стабильность время существование реальность
    язык математика искусство эстетика онтология открытый открытые открытых
    нерешенный нерешённый нерешенные нерешённые что почему как ли когда какие
    более менее разные одинаковые
    """.split()
)


def screen_ordinary_abstract_candidate(candidate, sources):
    baseline = screen_abstract_candidate(candidate, sources)
    if baseline.decision is not PrivacyScreenDecision.ALLOW:
        return PrivacyScreenResult(
            ORDINARY_SCREEN_VERSION, baseline.decision, baseline.reason
        )
    words = _normalized_words(candidate.value)
    if (
        not 3 <= len(words) <= 64
        or any(word not in _WORDS for word in words)
        or re.search(r"[^\w\s.,;:!?—–-]", candidate.value) is not None
    ):
        return PrivacyScreenResult(
            ORDINARY_SCREEN_VERSION,
            PrivacyScreenDecision.UNCERTAIN,
            "ordinary_generality_uncertain",
        )
    return PrivacyScreenResult(
        ORDINARY_SCREEN_VERSION, PrivacyScreenDecision.ALLOW, "allowed"
    )

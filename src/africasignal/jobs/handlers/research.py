"""Research handlers are discovered by the existing worker registry."""

from africasignal.jobs.handlers import JobContext, register
from africasignal.research import corrections, executor, guardian, planner


@register("plan_research")
def plan_research(ctx: JobContext) -> None:
    planner.plan(ctx.session)


@register("research_question")
def research_question(ctx: JobContext) -> None:
    executor.research(ctx)


@register("judge_research")
def judge_research(ctx: JobContext) -> None:
    executor.decide(ctx)


@register("apply_research")
def apply_research(ctx: JobContext) -> None:
    corrections.apply(ctx)


@register("verify_research")
def verify_research(ctx: JobContext) -> None:
    corrections.verify(ctx)


@register("check_research")
def check_research(ctx: JobContext) -> None:
    guardian.check(ctx.session)

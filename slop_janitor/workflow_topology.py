from __future__ import annotations

from slop_janitor.models import Stage

FIXED_IMPROVE_SKILL = "execplan-improve"
FIXED_REVIEW_SKILL = "review-recent-work"


def stage_label(base_label: str, *, cycle_index: int, cycles: int) -> str:
    if cycles == 1:
        return base_label
    return f"cycle-{cycle_index}-{base_label}"


def build_follow_up_stages(
    *,
    cycle_index: int,
    cycles: int,
    improvement_count: int,
    review_count: int,
    skill_paths: dict[str, str],
    label_prefix: str | None = None,
) -> list[Stage]:
    def label(value: str) -> str:
        return f"{label_prefix}-{value}" if label_prefix else stage_label(value, cycle_index=cycle_index, cycles=cycles)

    return [
        *[
            Stage(
                label=label(f"{FIXED_IMPROVE_SKILL}-{index}"),
                skill_name=FIXED_IMPROVE_SKILL,
                skill_path=skill_paths[FIXED_IMPROVE_SKILL],
                text=f"${FIXED_IMPROVE_SKILL} improve the active work-item ExecPlan and rewrite it in place",
            )
            for index in range(1, improvement_count + 1)
        ],
        Stage(
            label=label("implement-execplan"),
            skill_name="implement-execplan",
            skill_path=skill_paths["implement-execplan"],
            text="$implement-execplan implement the active work-item ExecPlan",
        ),
        *[
            Stage(
                label=label(f"{FIXED_REVIEW_SKILL}-{index}"),
                skill_name=FIXED_REVIEW_SKILL,
                skill_path=skill_paths[FIXED_REVIEW_SKILL],
                text=(
                    f"${FIXED_REVIEW_SKILL} review the most recently implemented work-item ExecPlan"
                    if not label_prefix or index == review_count
                    else (
                        f"${FIXED_REVIEW_SKILL} review the most recently implemented work-item ExecPlan "
                        "but do not reconcile or advance any parent meta-plan in this non-final review pass"
                    )
                ),
            )
            for index in range(1, review_count + 1)
        ],
    ]


def build_refactor_stages(
    prompt: str | None,
    *,
    cycles: int,
    improvement_count: int,
    review_count: int,
    skill_paths: dict[str, str],
    default_refactor_prompt: str,
) -> list[Stage]:
    stages: list[Stage] = []
    for cycle_index in range(1, cycles + 1):
        refactor_prompt = prompt or default_refactor_prompt
        stages.extend(
            [
                Stage(
                    label=stage_label("find-refactor-candidates", cycle_index=cycle_index, cycles=cycles),
                    skill_name="find-refactor-candidates",
                    skill_path=skill_paths["find-refactor-candidates"],
                    text=f"$find-refactor-candidates {refactor_prompt}",
                ),
                Stage(
                    label=stage_label("select-refactor", cycle_index=cycle_index, cycles=cycles),
                    skill_name="select-refactor",
                    skill_path=skill_paths["select-refactor"],
                    text=(
                        "$select-refactor pressure-test the active shortlist, lock the best refactor decision, "
                        "and stop before planning."
                    ),
                ),
                Stage(
                    label=stage_label("execplan-create", cycle_index=cycle_index, cycles=cycles),
                    skill_name="execplan-create",
                    skill_path=skill_paths["execplan-create"],
                    text=(
                        "$execplan-create create an ExecPlan for the active refactor work item and write it into "
                        "that work item"
                    ),
                ),
            ]
        )
        stages.extend(
            build_follow_up_stages(
                cycle_index=cycle_index,
                cycles=cycles,
                improvement_count=improvement_count,
                review_count=review_count,
                skill_paths=skill_paths,
            )
        )
    return stages


def build_builder_stages(
    prompt: str,
    *,
    slices: int,
    improvement_count: int,
    review_count: int,
    skill_paths: dict[str, str],
) -> list[Stage]:
    stages = [
        Stage(
            label="create-meta-plan",
            skill_name="create-meta-plan",
            skill_path=skill_paths["create-meta-plan"],
            text=(
                f"$create-meta-plan turn this project brief into exactly {slices} bounded slices, "
                "write the parent meta-plan under .agent/meta-plans/, mark the first slice active, "
                f"and do not create child work items or ExecPlans yet: {prompt}"
            ),
        )
    ]
    for slice_index in range(1, slices + 1):
        label_prefix = f"slice-{slice_index}"
        stages.append(
            Stage(
                label=f"{label_prefix}-execplan-create",
                skill_name="execplan-create",
                skill_path=skill_paths["execplan-create"],
                text="$execplan-create create an ExecPlan for the active meta-plan slice and write it into a child work item",
            )
        )
        stages.extend(
            build_follow_up_stages(
                cycle_index=slice_index,
                cycles=slices,
                improvement_count=improvement_count,
                review_count=review_count,
                skill_paths=skill_paths,
                label_prefix=label_prefix,
            )
        )
    return stages


def build_existing_meta_plan_builder_stages(
    *,
    slices: int,
    improvement_count: int,
    review_count: int,
    skill_paths: dict[str, str],
) -> list[Stage]:
    stages: list[Stage] = []
    for slice_index in range(1, slices + 1):
        label_prefix = f"slice-{slice_index}"
        stages.append(
            Stage(
                label=f"{label_prefix}-execplan-create",
                skill_name="execplan-create",
                skill_path=skill_paths["execplan-create"],
                text="$execplan-create create an ExecPlan for the active meta-plan slice and write it into a child work item",
            )
        )
        stages.extend(
            build_follow_up_stages(
                cycle_index=slice_index,
                cycles=slices,
                improvement_count=improvement_count,
                review_count=review_count,
                skill_paths=skill_paths,
                label_prefix=label_prefix,
            )
        )
    return stages


def planning_stage_count() -> int:
    return 3


def stages_per_cycle(*, improvement_count: int, review_count: int) -> int:
    return planning_stage_count() + improvement_count + review_count + 1


def builder_stages_per_slice(*, improvement_count: int, review_count: int) -> int:
    return 1 + improvement_count + 1 + review_count


def builder_slice_number_for_stage_index(
    stage_index: int,
    *,
    improvement_count: int,
    review_count: int,
    includes_meta_plan_creation: bool = True,
) -> int | None:
    first_slice_stage_index = 2 if includes_meta_plan_creation else 1
    if stage_index < first_slice_stage_index:
        return None
    return ((stage_index - first_slice_stage_index) // builder_stages_per_slice(
        improvement_count=improvement_count,
        review_count=review_count,
    )) + 1


def builder_slice_stage_position(
    stage_index: int,
    *,
    improvement_count: int,
    review_count: int,
    includes_meta_plan_creation: bool = True,
) -> int | None:
    first_slice_stage_index = 2 if includes_meta_plan_creation else 1
    if stage_index < first_slice_stage_index:
        return None
    return ((stage_index - first_slice_stage_index) % builder_stages_per_slice(
        improvement_count=improvement_count,
        review_count=review_count,
    )) + 1


def cycle_number_for_stage_index(stage_index: int, *, improvement_count: int, review_count: int) -> int:
    return ((stage_index - 1) // stages_per_cycle(
        improvement_count=improvement_count,
        review_count=review_count,
    )) + 1


def cycle_stage_position(stage_index: int, *, improvement_count: int, review_count: int) -> int:
    return ((stage_index - 1) % stages_per_cycle(
        improvement_count=improvement_count,
        review_count=review_count,
    )) + 1


def final_planning_stage_position(*, improvement_count: int) -> int:
    return planning_stage_count() + improvement_count


def implementation_stage_position(*, improvement_count: int) -> int:
    return final_planning_stage_position(improvement_count=improvement_count) + 1


def is_cycle_start_stage_index(stage_index: int, *, improvement_count: int, review_count: int) -> bool:
    return cycle_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    ) == 1


def is_final_planning_stage_index(stage_index: int, *, improvement_count: int, review_count: int) -> bool:
    return cycle_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    ) == final_planning_stage_position(improvement_count=improvement_count)


def is_implementation_stage_index(stage_index: int, *, improvement_count: int, review_count: int) -> bool:
    return cycle_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    ) == implementation_stage_position(improvement_count=improvement_count)


def is_final_review_stage_index(stage_index: int, *, improvement_count: int, review_count: int) -> bool:
    if review_count == 0:
        return False
    return cycle_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    ) == stages_per_cycle(
        improvement_count=improvement_count,
        review_count=review_count,
    )


def is_follow_on_review_stage_index(stage_index: int, *, improvement_count: int, review_count: int) -> bool:
    if review_count <= 1:
        return False
    return cycle_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    ) > implementation_stage_position(improvement_count=improvement_count) + 1


def stage_should_checkpoint(stage_index: int, *, improvement_count: int, review_count: int) -> bool:
    return any(
        (
            is_final_planning_stage_index(
                stage_index,
                improvement_count=improvement_count,
                review_count=review_count,
            ),
            is_implementation_stage_index(
                stage_index,
                improvement_count=improvement_count,
                review_count=review_count,
            ),
            is_final_review_stage_index(
                stage_index,
                improvement_count=improvement_count,
                review_count=review_count,
            ),
        )
    )


def checkpoint_message_for_stage(
    stage_label: str,
    *,
    stage_index: int,
    improvement_count: int,
    review_count: int,
) -> str | None:
    if stage_should_checkpoint(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    ):
        return f"slop-janitor: after {stage_label}"
    return None


def builder_stage_should_checkpoint(
    stage_index: int,
    *,
    improvement_count: int,
    review_count: int,
    includes_meta_plan_creation: bool = True,
) -> bool:
    if includes_meta_plan_creation and stage_index == 1:
        return True
    position = builder_slice_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
        includes_meta_plan_creation=includes_meta_plan_creation,
    )
    if position is None:
        return False
    final_planning_position = 1 + improvement_count
    implementation_position = final_planning_position + 1
    final_review_position = builder_stages_per_slice(
        improvement_count=improvement_count,
        review_count=review_count,
    )
    return position in {final_planning_position, implementation_position, final_review_position}


def builder_checkpoint_message_for_stage(
    stage_label: str,
    *,
    stage_index: int,
    improvement_count: int,
    review_count: int,
    includes_meta_plan_creation: bool = True,
) -> str | None:
    if builder_stage_should_checkpoint(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
        includes_meta_plan_creation=includes_meta_plan_creation,
    ):
        return f"slop-janitor: after {stage_label}"
    return None


def stage_should_start_clean(*, stage_index: int, improvement_count: int, review_count: int) -> bool:
    return not is_follow_on_review_stage_index(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    )


def builder_stage_should_start_clean(
    *,
    stage_index: int,
    improvement_count: int,
    review_count: int,
    includes_meta_plan_creation: bool = True,
) -> bool:
    position = builder_slice_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
        includes_meta_plan_creation=includes_meta_plan_creation,
    )
    if position is None:
        return True
    review_start_position = 1 + improvement_count + 1 + 1
    return position < review_start_position


def stage_should_end_clean(*, stage_index: int, improvement_count: int, review_count: int) -> bool:
    return stage_should_checkpoint(
        stage_index=stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    )


def builder_stage_should_end_clean(
    *,
    stage_index: int,
    improvement_count: int,
    review_count: int,
    includes_meta_plan_creation: bool = True,
) -> bool:
    return builder_stage_should_checkpoint(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
        includes_meta_plan_creation=includes_meta_plan_creation,
    )


def terminal_phase_label(*, stage_index: int, improvement_count: int, review_count: int) -> str:
    position = cycle_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
    )
    if position == 1:
        return "Refactor Discovery"
    if position == 2:
        return "Refactor Selection"
    if position == 3:
        return "ExecPlan Planning"
    if position <= final_planning_stage_position(improvement_count=improvement_count):
        improve_index = position - planning_stage_count()
        return f"Improvement Pass {improve_index}/{improvement_count}"
    if position == implementation_stage_position(improvement_count=improvement_count):
        return "Implementation"
    review_index = position - implementation_stage_position(improvement_count=improvement_count)
    return f"Review Pass {review_index}/{review_count}"


def builder_terminal_phase_label(
    *,
    stage_index: int,
    improvement_count: int,
    review_count: int,
    includes_meta_plan_creation: bool = True,
) -> str:
    if includes_meta_plan_creation and stage_index == 1:
        return "MetaPlan Creation"
    position = builder_slice_stage_position(
        stage_index,
        improvement_count=improvement_count,
        review_count=review_count,
        includes_meta_plan_creation=includes_meta_plan_creation,
    )
    if position == 1:
        return "Slice ExecPlan Planning"
    if position is not None and position <= 1 + improvement_count:
        improve_index = position - 1
        return f"Slice Improvement Pass {improve_index}/{improvement_count}"
    if position == 1 + improvement_count + 1:
        return "Slice Implementation"
    review_index = (position or 0) - (1 + improvement_count + 1)
    return f"Slice Review Pass {review_index}/{review_count}"

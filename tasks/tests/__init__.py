def __getattr__(name):
    if name == "PlanningWorkflowTests":
        from .test_planning_workflow import PlanningWorkflowTests
        return PlanningWorkflowTests
    if name in ("AutomaticModeTests", "AutomationTests"):
        from .test_automation import AutomationTests
        return AutomationTests
    raise AttributeError(name)
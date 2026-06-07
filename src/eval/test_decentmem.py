import sys
sys.path.insert(0, r"d:\VscodeProject\repobugfix_complete_project")
from decentmem_integration import demo_dual_pool_init, demo_router_decisions, demo_llm_judge, demo_grpo_integration

print("=" * 60)
print("DECENTMEM Integration Demo")
print("=" * 60)

demo_dual_pool_init()
demo_router_decisions()
demo_llm_judge()
demo_grpo_integration()

print("\n" + "=" * 60)
print("All demos completed successfully!")
print("=" * 60)

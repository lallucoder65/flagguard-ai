from flagsmith import Flagsmith

flagsmith = Flagsmith(environment_key="BrTukFPrbc6d5jGFEy2K8w")
flags = flagsmith.get_environment_flags()

def process_payment(user_id: str):
    # Healthy flag: active in Flagsmith + used in code
    if flags.is_feature_enabled("active_checkout"):
        return "New v2 checkout flow"
    return "Legacy checkout"

def search_products(query: str):
    # Ghost flag: referenced in code, completely missing from Flagsmith!
    if flags.is_feature_enabled("ghost_ai_search"):
        return f"AI vector search for {query}"
    return f"Basic SQL search for {query}"

def calculate_shipping(total: float) -> float:
    if flags.is_feature_enabled("legacy_cart_v1"):
        # Deprecated flat-rate logic from Q1
        return 99.0
    return 0.0 if total >= 499 else 49.0   
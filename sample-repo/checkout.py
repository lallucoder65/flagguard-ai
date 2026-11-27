from flagsmith import Flagsmith
flagsmith = Flagsmith(environment_key='BrTukFPrbc6d5jGFEy2K8w')
flags = flagsmith.get_environment_flags()
CHECKOUT_V2_FLAG = 'active_checkout'

def process_payment(amount: float):
    if flags.is_feature_enabled(CHECKOUT_V2_FLAG):
        return 'New Stripe v2 Checkout'
    return 'Legacy Checkout'

def search_products(query: str):
    return f'Basic SQL search for {query}'

def calculate_shipping(total: float) -> float:
    return 0.0 if total >= 499 else 49.0

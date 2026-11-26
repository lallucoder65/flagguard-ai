def get_theme(flags):
    # Drifted flag: ON in Dev, OFF in Prod
    if flags.is_feature_enabled("drift_dark_mode"):
        return "dark"
    return "light"
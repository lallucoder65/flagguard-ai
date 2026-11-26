import flagsmith from "@flagsmith/flagsmith";

export function HeaderBanner() {
  return flagsmith.hasFeature("active_checkout") ? <div>Express Checkout</div> : null;
}
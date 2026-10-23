"""Subscription plans for using the PowerHouse Gym Owner App itself.

Single source of truth: the public pricing page (/pricing) and the in-app
Plan & Billing screen both render from GET /api/plans, which serves this.

Prices (INR) are set from live market research of Indian gym software —
competitors run ₹399–₹1,999/month (GymMunshi ₹399/₹799/₹1,299, Fitzenix
₹499/₹999/₹1,999, GymExec ₹499/₹999/₹2,499, FitnessDesk ₹999/₹2,499/₹4,999);
yearly billing across the market is roughly 2 months free.
"""

SUBSCRIPTION_PLANS = [
    {
        "id": "starter",
        "name": "Standard",
        "monthly": 1599,
        "yearly": 15990,
        "member_limit": 69,
        "badge": None,
        "features": [
            "Up to 69 members",
            "No-Show Red List",
            "QR Check-In",
            "Renewals",
            "Search in every section",
            "AUTO mode",
            "1 owner login",
        ],
    },
    {
        "id": "pro",
        "name": "Premium",
        "monthly": 1899,
        "yearly": 18990,
        "member_limit": None,  # unlimited
        "badge": "Most Popular",
        "features": [
            "Unlimited members",
            "Everything in Standard",
            "AUTO + MANUAL modes",
            "Renewal history & reports",
            "Priority support",
        ],
    },
]


def get_subscription_plan(plan_id: str):
    return next((p for p in SUBSCRIPTION_PLANS if p["id"] == plan_id), None)

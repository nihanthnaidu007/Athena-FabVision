"""Forms for the dashboard app."""

from django import forms

from assistant.models import ApiKey


class ApiKeyCreateForm(forms.Form):
    """Create-form for hashed API keys (hashing itself stays in ApiKey.generate)."""

    name = forms.CharField(max_length=100)
    rate_limit_tier = forms.ChoiceField(
        choices=ApiKey.RateTier.choices,
        initial=ApiKey.RateTier.STANDARD,
    )

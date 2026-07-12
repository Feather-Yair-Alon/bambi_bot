from app.services.payment_links import is_approved_dynamic_payment_url


def test_allows_dynamic_mbapps_payment_url() -> None:
    assert (
        is_approved_dynamic_payment_url(
            "https://6a09b3ab-e66c-64a7-7dbc-06c797b56505.mbapps.co.il/apps/mybooks/payment-btn-page?cls=PaymentBtns&oid=abc123"
        )
        is True
    )


def test_blocks_unapproved_payment_url() -> None:
    assert (
        is_approved_dynamic_payment_url("https://evil.example.com/apps/mybooks/payment-btn-page?cls=PaymentBtns&oid=abc123")
        is False
    )


def test_blocks_payment_url_with_duplicate_or_invalid_object_id() -> None:
    assert (
        is_approved_dynamic_payment_url(
            "https://6a09b3ab-e66c-64a7-7dbc-06c797b56505.mbapps.co.il/apps/mybooks/payment-btn-page?cls=PaymentBtns&oid=ok&oid=evil"
        )
        is False
    )
    assert (
        is_approved_dynamic_payment_url(
            "https://6a09b3ab-e66c-64a7-7dbc-06c797b56505.mbapps.co.il/apps/mybooks/payment-btn-page?cls=PaymentBtns&oid=https://evil.example"
        )
        is False
    )

from decimal import Decimal
from django.utils import timezone
from core.models import (
    Tenant, ChannelConnection, SalesChannel, SyncRun,
    ChannelOrder, ChannelSnapshot, Product, Location
)
from core.services.inventory import apply_movement
from core.services.gl import post_cogs


def _shopify_default_location(tenant: Tenant) -> Location:
    loc, _ = Location.objects.get_or_create(
        tenant=tenant, name="Shopify Warehouse",
        defaults={"type": "WAREHOUSE"}
    )
    return loc


def fake_fetch_shopify_orders(connection: ChannelConnection):
    now = timezone.now()
    return [
        {
            "id": "SHP-10001",
            "processed_at": now.isoformat(),
            "line_items": [{"sku": "SKU-001", "quantity": 1}, {"sku": "SKU-002", "quantity": 2}],
        }
    ]


def fake_fetch_shopify_inventory_snapshot(connection: ChannelConnection):
    return [{"sku": "SKU-001", "quantity": 10}, {"sku": "SKU-002", "quantity": 5}]


def sync_shopify_for_tenant(tenant: Tenant):
    conn = ChannelConnection.objects.filter(tenant=tenant, channel=SalesChannel.SHOPIFY).first()
    if not conn:
        return "No Shopify connection configured."

    run = SyncRun.objects.create(tenant=tenant, channel=SalesChannel.SHOPIFY)

    try:
        location = _shopify_default_location(tenant)

        orders = fake_fetch_shopify_orders(conn)
        for o in orders:
            external_id = str(o["id"])
            processed_at = timezone.datetime.fromisoformat(o["processed_at"].replace("Z", "+00:00"))

            obj, created = ChannelOrder.objects.get_or_create(
                tenant=tenant, channel=SalesChannel.SHOPIFY, external_order_id=external_id,
                defaults={"processed_at": processed_at, "payload": o}
            )
            if not created:
                continue

            cogs_total = Decimal("0.00")
            for li in o.get("line_items", []):
                sku = li["sku"]
                qty = Decimal(str(li["quantity"]))

                product = Product.objects.filter(tenant=tenant, sku=sku).first()
                if not product:
                    continue

                movement = apply_movement(
                    tenant=tenant,
                    product=product,
                    location=location,
                    movement_type="SALE",
                    qty_delta=(qty * Decimal("-1")),
                    ref_type="ORDER",
                    ref_id=external_id,
                    notes="Shopify order sync"
                )
                cogs_total += -(movement.value or Decimal("0.00"))

            # Expense COGS for the synced order.
            post_cogs(tenant, cogs_total, external_id)

        snap = fake_fetch_shopify_inventory_snapshot(conn)
        for row in snap:
            ChannelSnapshot.objects.create(
                tenant=tenant,
                channel=SalesChannel.SHOPIFY,
                sku=row["sku"],
                quantity=Decimal(str(row["quantity"])),
                as_of=timezone.now()
            )

        run.status = "SUCCESS"
        run.detail = f"Orders: {len(orders)}, Snapshot rows: {len(snap)}"
        run.finished_at = timezone.now()
        run.save()
        return run.detail

    except Exception as e:
        run.status = "FAILED"
        run.detail = str(e)
        run.finished_at = timezone.now()
        run.save()
        raise

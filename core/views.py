import re
from decimal import Decimal, InvalidOperation
from django.db import transaction, IntegrityError
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

from core.models import (
    Tenant, Location, PurchaseOrder, PurchaseOrderLine, PurchaseOrderAmendment, Shipment, ShipmentLine, Container, ShipmentEvent,
    InventoryBalance, InventoryMovement, ChannelSnapshot, SalesChannel, Product,
    Supplier, ChannelConnection, SalesOrder, SalesOrderLine,
    ProductBarcode, UnitOfMeasure, UOMConversion, BillOfMaterials, BillOfMaterialsLine,
    InventoryTransfer, InventoryTransferLine,
    GoodsReceipt, GoodsReceiptLine, LandedCostCharge,
    SupplierInvoice, SupplierInvoiceLine,
    ReturnAuthorization, ReturnLine,
    TaxCode, Customer, CustomerInvoice, CustomerInvoiceLine,
    GLAccount, JournalEntry, Payment, PaymentAllocation, VatReturn
)
from core.forms import (
    PurchaseOrderForm, PurchaseOrderLineFormSet,
    ShipmentUpdateForm, ProductForm, SupplierForm,
    LocationForm, ChannelConnectionForm,
    SalesOrderForm, SalesOrderLineFormSet, TenantSettingsForm,
    UnitOfMeasureForm, UOMConversionForm, BillOfMaterialsForm, BOMLineFormSet,
    InventoryTransferForm, InventoryTransferLineFormSet,
    GoodsReceiptForm, GoodsReceiptLineFormSet, LandedCostChargeForm,
    SupplierInvoiceForm, SupplierInvoiceLineFormSet,
    ReturnAuthorizationForm, ReturnLineFormSet,
    TaxCodeForm, CustomerForm,
    CustomerInvoiceForm, CustomerInvoiceLineFormSet,
    GLAccountForm, ReceiptForm, SupplierPaymentForm
)
from core.services.inventory import apply_movement, reserve_stock, release_reservations
from core.services.bom import explode_product
from core.services.gl import post_customer_invoice, post_supplier_invoice, post_payment, post_inventory_receipt, post_cogs
from core.services import reports as reports_service
from core.services import vat as vat_service
from django.db.utils import OperationalError
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from core.auth import role_required, ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_SALES, ROLE_FINANCE, ROLE_READONLY




def _get_default_tenant(request=None):
    # Resolve the active tenant for the request's user via their profile.
    # Falls back to the first tenant for users without a profile (e.g.
    # the initial superuser) so existing single-tenant setups keep working.
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        profile = getattr(user, "profile", None)
        if profile is not None:
            return profile.tenant
    return Tenant.objects.order_by("id").first()


def _generate_po_number():
    return "PO-" + timezone.now().strftime("%Y%m%d-%H%M%S-%f")


def _default_vat_rate(tenant):
    """Standard VAT rate for the tenant (from the 'STD' tax code), default 20%."""
    code = TaxCode.objects.filter(tenant=tenant, code="STD", is_active=True).first()
    return code.rate if code else Decimal("0.20")


@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_FINANCE, ROLE_READONLY])

def po_list(request):
    tenant = _get_default_tenant(request)
    pos = PurchaseOrder.objects.filter(tenant=tenant).order_by("-created_at")
    return render(request, "po_list.html", {"tenant": tenant, "pos": pos})

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_FINANCE, ROLE_READONLY])

def po_print(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)
    
    subtotal = sum((line.line_total for line in po.lines.all()), Decimal("0.00"))
    vat_rate = _default_vat_rate(tenant)
    vat_amount = subtotal * vat_rate
    total = subtotal + vat_amount

    return render(request, "po_print.html", {
        "tenant": tenant,
        "po": po,
        "subtotal": subtotal,
        "vat_amount": vat_amount,
        "total": total,
        "vat_rate_percent": vat_rate * 100,
    })



@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])
def po_send(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)

    if request.method != "POST":
        return redirect("po_detail", po_id=po.id)

    if po.status in [PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.CLOSED]:
        messages.error(request, "Cannot send a cancelled/closed PO.")
        return redirect("po_detail", po_id=po.id)

    supplier_email = getattr(po.supplier, "email", None)
    if not supplier_email:
        messages.error(request, "Supplier has no email address. Add one and try again.")
        return redirect("po_detail", po_id=po.id)

    subject = f"Purchase Order {po.po_number}"
    # Lightweight: send HTML (print page) as email body. PDF export can be added later.
    html_body = render_to_string("po_email.html", {"tenant": tenant, "po": po})
    msg = EmailMessage(subject=subject, body=html_body, to=[supplier_email])
    msg.content_subtype = "html"
    try:
        msg.send(fail_silently=False)
    except Exception as e:
        messages.error(request, f"Email failed: {e}")
        return redirect("po_detail", po_id=po.id)

    po.sent_to = supplier_email
    po.sent_at = timezone.now()
    po.sent_subject = subject
    po.status = PurchaseOrder.Status.SENT if po.status in [PurchaseOrder.Status.SUBMITTED, PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.APPROVAL_PENDING] else po.status
    po.save(update_fields=["sent_to", "sent_at", "sent_subject", "status"])

    messages.success(request, f"PO emailed to {supplier_email}.")
    return redirect("po_detail", po_id=po.id)



@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])
def po_amend(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)

    if request.method != "POST":
        return redirect("po_detail", po_id=po.id)

    if po.status in [PurchaseOrder.Status.DRAFT]:
        messages.info(request, "Draft POs can be edited directly.")
        return redirect("po_detail", po_id=po.id)

    if po.status in [PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.CLOSED]:
        messages.error(request, "Cannot amend a cancelled/closed PO.")
        return redirect("po_detail", po_id=po.id)

    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Amendment reason is required.")
        return redirect("po_detail", po_id=po.id)

    # New version needs a unique po_number (Meta.unique_together = tenant, po_number).
    # Derive a stable base by stripping any prior "-vN" suffix, then re-version.
    new_version = po.version + 1
    base_number = re.sub(r"-v\d+$", "", po.po_number)
    new_number = f"{base_number}-v{new_version}"

    with transaction.atomic():
        # Create new version
        new_po = PurchaseOrder.objects.create(
            tenant=tenant,
            po_number=new_number,
            supplier=po.supplier,
            currency_code=po.currency_code,
            version=new_version,
            supersedes=po,
            is_current=True,
            status=PurchaseOrder.Status.DRAFT,
            expected_date=po.expected_date,
            notes=po.notes,
        )
        for line in po.lines.all():
            PurchaseOrderLine.objects.create(
                po=new_po,
                product=line.product,
                ordered_qty=line.ordered_qty,
                received_qty=line.received_qty,
                unit_cost=line.unit_cost,
            )

        po.is_current = False
        po.save(update_fields=["is_current"])

        PurchaseOrderAmendment.objects.create(
            tenant=tenant,
            from_po=po,
            to_po=new_po,
            reason=reason,
            created_by=request.user,
        )

    messages.success(request, f"Created PO amendment v{new_po.version}. Update it and submit again.")
    return redirect("po_detail", po_id=new_po.id)



@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])
def po_cancel(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)

    if request.method != "POST":
        return redirect("po_detail", po_id=po.id)

    if po.status in [PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.CLOSED]:
        messages.info(request, "PO is already cancelled/closed.")
        return redirect("po_detail", po_id=po.id)

    reason = request.POST.get("reason", "").strip() or "Cancelled"
    any_received = any((l.received_qty > Decimal("0.00") for l in po.lines.all()))
    if any_received:
        # Cancel remaining qty only (close open qty by setting ordered=received for remaining)
        for l in po.lines.all():
            if l.open_qty > Decimal("0.00"):
                l.ordered_qty = l.received_qty
                l.save(update_fields=["ordered_qty"])
        po.status = PurchaseOrder.Status.CLOSED if all((l.open_qty == Decimal("0.00") for l in po.lines.all())) else po.status
        po.cancelled_reason = reason
        po.save(update_fields=["status", "cancelled_reason"])
        messages.warning(request, "Partial receipts exist. Cancelled remaining quantities (PO effectively closed for open lines).")
        return redirect("po_detail", po_id=po.id)

    po.status = PurchaseOrder.Status.CANCELLED
    po.cancelled_reason = reason
    po.save(update_fields=["status", "cancelled_reason"])
    messages.success(request, "PO cancelled.")
    return redirect("po_detail", po_id=po.id)



def _safe_default_tenant(request=None):
    try:
        return _get_default_tenant(request)
    except OperationalError:
        return None

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_SALES, ROLE_FINANCE, ROLE_READONLY])

def landing(request):
    tenant = _safe_default_tenant(request)
    if not tenant:
        return render(request, "landing.html", {
            "tenant": None,
            "needs_setup": True,
        })

    today = timezone.localdate()

    # ----- Financial KPIs (from the GL + reports services) -----
    balances = reports_service.account_balances(tenant)
    by_code = {acc.code: vals["balance"] for acc, vals in balances.items()}
    cash = by_code.get("1050", Decimal("0.00"))
    pnl = reports_service.profit_and_loss(tenant)
    ar_total = reports_service.aged_receivables(tenant)["total"]
    ap_total = reports_service.aged_payables(tenant)["total"]
    stock_value = reports_service.stock_valuation(tenant)["total"]

    kpis = {
        "cash": cash,
        "inventory_value": stock_value,
        "receivables": ar_total,
        "payables": ap_total,
        "net_profit": pnl["net_profit"],
        "revenue": pnl["income_total"],
    }

    # ----- Operational counts -----
    open_po_statuses = [
        PurchaseOrder.Status.SUBMITTED, PurchaseOrder.Status.APPROVAL_PENDING,
        PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.SENT,
        PurchaseOrder.Status.IN_TRANSIT, PurchaseOrder.Status.PARTIALLY_RECEIVED,
    ]
    counts = {
        "open_pos": PurchaseOrder.objects.filter(tenant=tenant, status__in=open_po_statuses).count(),
        "in_transit": Shipment.objects.filter(tenant=tenant, status__in=[Shipment.Status.IN_TRANSIT, Shipment.Status.PICKED_UP]).count(),
        "sales_orders": SalesOrder.objects.filter(tenant=tenant).count(),
        "products": Product.objects.filter(tenant=tenant).count(),
    }

    # ----- Action-required alerts -----
    awaiting_approval = PurchaseOrder.objects.filter(tenant=tenant, status=PurchaseOrder.Status.APPROVAL_PENDING).count()
    overdue_invoices = CustomerInvoice.objects.filter(tenant=tenant, status=CustomerInvoice.Status.ISSUED, due_date__lt=today).count()
    out_of_stock = InventoryBalance.objects.filter(tenant=tenant, on_hand__lte=Decimal("0.00")).count()
    alerts = []
    if awaiting_approval:
        alerts.append({"icon": "patch-question", "level": "warning", "text": f"{awaiting_approval} purchase order(s) awaiting approval", "url": "/po/"})
    if overdue_invoices:
        alerts.append({"icon": "exclamation-octagon", "level": "danger", "text": f"{overdue_invoices} overdue customer invoice(s)", "url": "/reports/aged-receivables/"})
    if out_of_stock:
        alerts.append({"icon": "box", "level": "secondary", "text": f"{out_of_stock} stock line(s) at or below zero", "url": "/inventory/"})

    # ----- Recent activity -----
    recent_pos = PurchaseOrder.objects.filter(tenant=tenant).select_related("supplier").order_by("-created_at")[:6]
    recent_payments = Payment.objects.filter(tenant=tenant).select_related("customer", "supplier").order_by("-created_at")[:6]

    return render(request, "landing.html", {
        "tenant": tenant,
        "needs_setup": False,
        "kpis": kpis,
        "counts": counts,
        "alerts": alerts,
        "recent_pos": recent_pos,
        "recent_payments": recent_payments,
    })


@transaction.atomic
@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])
def po_create(request):
    tenant = _get_default_tenant(request)
    if not tenant:
        return render(request, "base.html", {"content": "Create a Tenant in admin first."})

    po = PurchaseOrder(tenant=tenant, po_number=_generate_po_number())

    if request.method == "POST":
        form = PurchaseOrderForm(request.POST, instance=po)
        formset = PurchaseOrderLineFormSet(request.POST, instance=po)

        if form.is_valid() and formset.is_valid():
            po = form.save(commit=False)
            po.tenant = tenant

            action = form.cleaned_data.get("action") or "save"
            po.status = PurchaseOrder.Status.SUBMITTED if action == "submit" else PurchaseOrder.Status.DRAFT

            # Save first so we have an ID for lines
            po.save()
            formset.save()

            # Set currency from supplier (per supplier currency)
            try:
                po.currency_code = po.supplier.currency_code or tenant.currency_code
            except Exception:
                po.currency_code = tenant.currency_code
            po.save()

            # Threshold-based approvals
            total = sum((l.line_total for l in po.lines.all()), Decimal("0.00"))
            threshold = getattr(tenant, "po_approval_threshold", Decimal("0.00")) or Decimal("0.00")
            if po.status == PurchaseOrder.Status.SUBMITTED and threshold > 0 and total > threshold:
                po.approval_required = True
                po.status = PurchaseOrder.Status.APPROVAL_PENDING
                po.save()
                messages.warning(
                    request,
                    f"PO submitted but requires approval (total {total} > threshold {threshold})."
                )

            # Create shipment record on submit only if approval not required
            if po.status == PurchaseOrder.Status.SUBMITTED and not getattr(po, "approval_required", False):
                dest = Location.objects.filter(tenant=tenant).order_by("id").first()
                if dest:
                    Shipment.objects.get_or_create(
                        tenant=tenant,
                        po=po,
                        defaults={
                            "from_supplier": po.supplier,
                            "destination": dest,
                            "status": Shipment.Status.CREATED,
                        },
                    )

            return redirect("po_detail", po_id=po.id)
    else:
        form = PurchaseOrderForm(instance=po)
        formset = PurchaseOrderLineFormSet(instance=po)

    return render(request, "po_create.html", {"tenant": tenant, "form": form, "formset": formset, "po": po})


@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE], [ROLE_ADMIN, ROLE_FINANCE])
@transaction.atomic
def po_approve(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)

    if request.method != "POST":
        return redirect("po_detail", po_id=po.id)

    if po.status not in [PurchaseOrder.Status.SUBMITTED, PurchaseOrder.Status.APPROVAL_PENDING]:
        messages.info(request, "PO is not awaiting approval.")
        return redirect("po_detail", po_id=po.id)

    po.approval_required = False
    po.approved_by = request.user
    po.approved_at = timezone.now()
    po.status = PurchaseOrder.Status.APPROVED
    po.save()

    # Ensure there is at least one shipment + shipment lines
    dest = Location.objects.filter(tenant=tenant).order_by("id").first()
    if dest:
        shipment, _ = Shipment.objects.get_or_create(
            tenant=tenant,
            po=po,
            defaults={"from_supplier": po.supplier, "destination": dest, "status": Shipment.Status.CREATED},
        )
        for pol in po.lines.all():
            ShipmentLine.objects.get_or_create(
                shipment=shipment,
                po_line=pol,
                defaults={"expected_qty": pol.open_qty},
            )

    messages.success(request, f"PO {po.po_number} approved.")
    return redirect("po_detail", po_id=po.id)

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_FINANCE, ROLE_READONLY])
def po_detail(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)

    subtotal = sum((line.line_total for line in po.lines.all()), Decimal("0.00"))
    vat_rate = _default_vat_rate(tenant)
    vat_amount = subtotal * vat_rate
    total = subtotal + vat_amount

    return render(request, "po_detail.html", {
        "tenant": tenant,
        "po": po,
        "subtotal": subtotal,
        "vat_amount": vat_amount,
        "total": total,
        "vat_rate_percent": vat_rate * 100,
    })


@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])
@transaction.atomic
def po_submit(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)

    if request.method != "POST":
        return redirect("po_detail", po_id=po.id)

    if po.status != PurchaseOrder.Status.DRAFT:
        messages.info(request, "Only Draft POs can be submitted.")
        return redirect("po_detail", po_id=po.id)

    # Set currency from supplier (per-supplier currency), fallback to tenant
    po.currency_code = getattr(po.supplier, "currency_code", None) or tenant.currency_code

    total = sum((l.line_total for l in po.lines.all()), Decimal("0.00"))
    threshold = getattr(tenant, "po_approval_threshold", Decimal("0.00")) or Decimal("0.00")

    po.approval_required = bool(threshold and threshold > 0 and total > threshold)
    po.status = PurchaseOrder.Status.APPROVAL_PENDING if po.approval_required else PurchaseOrder.Status.SUBMITTED
    po.save()

    # Always create at least 1 shipment on submit (planned), with shipment lines (expected qty = open qty)
    dest = Location.objects.filter(tenant=tenant).order_by("id").first()
    if not dest:
        messages.error(request, "Create at least one Location before submitting POs.")
        return redirect("po_detail", po_id=po.id)

    shipment, _ = Shipment.objects.get_or_create(
        tenant=tenant,
        po=po,
        defaults={
            "from_supplier": po.supplier,
            "destination": dest,
            "status": Shipment.Status.CREATED,
        },
    )

    # Create/refresh shipment lines from PO open qty
    for pol in po.lines.all():
        exp = pol.open_qty
        sl, _ = ShipmentLine.objects.get_or_create(
            shipment=shipment,
            po_line=pol,
            defaults={"expected_qty": exp},
        )
        # If draft submission and no receipts yet, keep expected in sync with ordered
        if sl.received_qty == Decimal("0.00"):
            sl.expected_qty = exp
            sl.save(update_fields=["expected_qty"])

    if po.approval_required:
        messages.warning(request, f"PO submitted. Approval required (total {total} > threshold {threshold}).")
    else:
        messages.success(request, f"PO {po.po_number} submitted.")
    return redirect("po_detail", po_id=po.id)

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_READONLY])
def shipment_list(request):
    tenant = _get_default_tenant(request)
    shipments = Shipment.objects.filter(tenant=tenant).select_related("po", "from_supplier", "destination").order_by("-created_at")
    return render(request, "shipments/shipment_list.html", {"tenant": tenant, "shipments": shipments})


@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_READONLY], [ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE])
def shipment_detail(request, shipment_id):
    tenant = _get_default_tenant(request)
    shipment = get_object_or_404(Shipment, id=shipment_id, tenant=tenant)
    po = shipment.po

    form = ShipmentUpdateForm(request.POST or None, instance=shipment)

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "update_shipment" and form.is_valid():
            shipment = form.save()
            # Keep PO in transit if any shipment is in transit-ish
            if shipment.status in [Shipment.Status.IN_TRANSIT, Shipment.Status.PICKED_UP]:
                po.status = PurchaseOrder.Status.IN_TRANSIT
                po.save(update_fields=["status"])
            messages.success(request, "Shipment updated.")
            return redirect("shipment_detail", shipment_id=shipment.id)

        if action == "allocate":
            # Validate first, then persist atomically - so an invalid value
            # gives a friendly message instead of a 500.
            try:
                with transaction.atomic():
                    for sl in shipment.lines.select_related("po_line", "po_line__product"):
                        raw = (request.POST.get(f"exp_{sl.id}") or "").strip()
                        if raw == "":
                            continue
                        try:
                            exp = Decimal(raw)
                        except InvalidOperation:
                            raise ValueError(f"Invalid quantity '{raw}' for {sl.po_line.product.sku}.")
                        if exp < sl.received_qty:
                            raise ValueError(
                                f"Expected qty cannot be below received for {sl.po_line.product.sku}."
                            )
                        sl.expected_qty = exp
                        sl.save(update_fields=["expected_qty"])
            except ValueError as e:
                messages.error(request, str(e))
                return redirect("shipment_detail", shipment_id=shipment.id)
            messages.success(request, "Allocation updated.")
            return redirect("shipment_detail", shipment_id=shipment.id)

        if action == "add_container":
            cn = (request.POST.get("container_number") or "").strip()
            if not cn:
                messages.error(request, "Container number is required.")
                return redirect("shipment_detail", shipment_id=shipment.id)
            Container.objects.get_or_create(
                shipment=shipment,
                container_number=cn,
                defaults={
                    "seal_number": (request.POST.get("seal_number") or "").strip() or None,
                    "mode": (request.POST.get("mode") or "").strip() or None,
                }
            )
            messages.success(request, "Container added.")
            return redirect("shipment_detail", shipment_id=shipment.id)

        if action == "add_event":
            event_type = (request.POST.get("event_type") or "").strip()
            if not event_type:
                messages.error(request, "Event type is required.")
                return redirect("shipment_detail", shipment_id=shipment.id)
            cont_id = (request.POST.get("container_id") or "").strip()
            cont = None
            if cont_id:
                cont = get_object_or_404(Container, id=cont_id, shipment=shipment)
            ShipmentEvent.objects.create(
                shipment=shipment,
                container=cont,
                event_type=event_type,
                status=(request.POST.get("status") or "").strip() or None,
                notes=(request.POST.get("notes") or "").strip() or None,
            )
            messages.success(request, "Event added.")
            return redirect("shipment_detail", shipment_id=shipment.id)

    # Derived
    lines = shipment.lines.select_related("po_line", "po_line__product").all()
    containers = shipment.containers.all()
    events = shipment.events.select_related("container").order_by("-occurred_at")
    return render(request, "shipments/shipment_detail.html", {
        "tenant": tenant,
        "po": po,
        "shipment": shipment,
        "form": form,
        "lines": lines,
        "containers": containers,
        "events": events,
    })

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE, ROLE_READONLY], [ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_WAREHOUSE])
def shipment_update(request, shipment_id):
    return shipment_detail(request, shipment_id)

@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE], [ROLE_ADMIN, ROLE_WAREHOUSE])
def receive_po(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)

    if po.approval_required or po.status == PurchaseOrder.Status.APPROVAL_PENDING:
        messages.error(request, "PO requires approval before receiving.")
        return redirect("po_detail", po_id=po.id)

    shipment_id = request.GET.get("shipment_id") or request.POST.get("shipment_id")
    if shipment_id:
        shipment = get_object_or_404(Shipment, id=shipment_id, tenant=tenant, po=po)
    else:
        shipment = po.shipments.order_by("-created_at").first()

    if not shipment:
        messages.error(request, "No shipment found for this PO. Submit the PO first.")
        return redirect("po_detail", po_id=po.id)

    if not shipment.lines.exists():
        messages.error(request, "This shipment has no planned lines. Allocate quantities to the shipment first.")
        return redirect("shipment_detail", shipment_id=shipment.id)

    dest_location = shipment.destination
    default_grn = "GRN-" + timezone.now().strftime("%Y%m%d-%H%M%S-%f")

    if request.method == "POST":
        grn_number = (request.POST.get("grn_number") or default_grn).strip()
        received_at = timezone.now()

        try:
            with transaction.atomic():
                receipt = GoodsReceipt.objects.create(
                    tenant=tenant,
                    po=po,
                    shipment=shipment,
                    grn_number=grn_number,
                    received_at=received_at,
                    received_to=dest_location,
                    attachment=request.FILES.get("attachment"),
                    status=GoodsReceipt.Status.DRAFT,
                )

                # Optional single landed cost (MVP)
                lc_name = (request.POST.get("landed_cost_name") or "").strip()
                lc_amount_raw = (request.POST.get("landed_cost_amount") or "").strip()
                if lc_name and lc_amount_raw:
                    try:
                        LandedCostCharge.objects.create(
                            tenant=tenant,
                            receipt=receipt,
                            name=lc_name,
                            amount=Decimal(lc_amount_raw),
                            currency_code=po.currency_code,
                        )
                    except Exception:
                        pass

                # Pass 1: validate + collect received lines and the goods total.
                received_lines = []
                goods_total = Decimal("0.00")
                for sl in shipment.lines.select_related("po_line", "po_line__product"):
                    qty_raw = (request.POST.get(f"recv_{sl.id}") or "").strip()
                    if not qty_raw:
                        continue
                    try:
                        qty = Decimal(qty_raw)
                    except InvalidOperation:
                        raise ValueError(f"Invalid quantity '{qty_raw}' for {sl.po_line.product.sku}.")
                    if qty <= 0:
                        continue
                    if qty > sl.open_qty:
                        raise ValueError(
                            f"Cannot receive more than open qty for {sl.po_line.product.sku}."
                        )
                    expiry_raw = (request.POST.get(f"expiry_{sl.id}") or "").strip()
                    expiry = None
                    if expiry_raw:
                        try:
                            expiry = timezone.datetime.fromisoformat(expiry_raw).date()
                        except Exception:
                            expiry = None
                    received_lines.append({
                        "sl": sl, "qty": qty,
                        "lot_code": (request.POST.get(f"lot_{sl.id}") or "").strip() or None,
                        "serial": (request.POST.get(f"serial_{sl.id}") or "").strip() or None,
                        "expiry": expiry,
                    })
                    goods_total += qty * sl.po_line.unit_cost

                if not received_lines:
                    # Nothing actually received: abort the whole transaction.
                    raise ValueError("Nothing received.")

                # Landed costs apportion across received value, raising unit cost.
                landed_total = sum((lc.amount for lc in receipt.landed_costs.all()), Decimal("0.00"))
                ratio = (landed_total / goods_total) if goods_total > 0 else Decimal("0.00")

                # Pass 2: create GRN lines, apply costed movements.
                inventory_value = Decimal("0.00")
                for item in received_lines:
                    sl = item["sl"]
                    qty = item["qty"]
                    base_cost = sl.po_line.unit_cost
                    landed_unit_cost = (base_cost * (Decimal("1") + ratio)).quantize(Decimal("0.0001"))

                    GoodsReceiptLine.objects.create(
                        receipt=receipt, po_line=sl.po_line, product=sl.po_line.product,
                        qty_received=qty, unit_cost=base_cost,
                        lot_code=item["lot_code"], serial_number=item["serial"], expiry_date=item["expiry"],
                    )
                    movement = apply_movement(
                        tenant=tenant,
                        product=sl.po_line.product,
                        location=dest_location,
                        movement_type=InventoryMovement.MovementType.RECEIVE,
                        qty_delta=qty,
                        ref_type="GRN",
                        ref_id=receipt.grn_number,
                        notes=f"Receipt against PO {po.po_number}",
                        lot_code=item["lot_code"], serial_number=item["serial"], expiry_date=item["expiry"],
                        unit_cost=landed_unit_cost,
                    )
                    # Actual capitalized value (standard products differ from cost).
                    inventory_value += movement.value or Decimal("0.00")
                    sl.received_qty += qty
                    sl.save(update_fields=["received_qty"])
                    pol = sl.po_line
                    pol.received_qty += qty
                    pol.save(update_fields=["received_qty"])

                # Capitalize: DR Inventory (at cost basis) / CR GRNI (goods) / CR
                # Accruals (landed) / +/- Purchase Price Variance (standard costing).
                post_inventory_receipt(tenant, goods_total, receipt.grn_number, user=request.user,
                                       entry_date=received_at.date(), landed_value=landed_total,
                                       inventory_value=inventory_value)

                # Update PO status
                if all((l.open_qty == Decimal("0.00") for l in po.lines.all())):
                    po.status = PurchaseOrder.Status.RECEIVED
                else:
                    po.status = PurchaseOrder.Status.PARTIALLY_RECEIVED
                po.save(update_fields=["status"])

                receipt.status = GoodsReceipt.Status.POSTED
                receipt.save(update_fields=["status"])
        except ValueError as e:
            # Validation failure (over-receipt / nothing received): roll back and re-prompt.
            messages.error(request, str(e))
            return redirect("receive_po", po_id=po.id)

        messages.success(request, "Receipt posted.")
        return redirect("po_detail", po_id=po.id)

    return render(request, "receive_po.html", {
        "tenant": tenant,
        "po": po,
        "shipment": shipment,
        "default_grn": default_grn,
    })

@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_FINANCE, ROLE_READONLY])
def inventory_list(request):
    tenant = _get_default_tenant(request)
    balances = (
        InventoryBalance.objects
        .filter(tenant=tenant)
        .select_related("product", "location")
        .order_by("product__sku", "location__name")
    )
    return render(request, "inventory_list.html", {"tenant": tenant, "balances": balances})


@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE, ROLE_READONLY])

def reconcile(request):
    tenant = _get_default_tenant(request)

    # Latest snapshot per SKU for Shopify (MVP)
    latest = {}
    for s in ChannelSnapshot.objects.filter(tenant=tenant, channel=SalesChannel.SHOPIFY).order_by("sku", "-as_of"):
        if s.sku not in latest:
            latest[s.sku] = s

    # SKUNOW totals per SKU (sum across locations)
    skunow_totals = {}
    for b in InventoryBalance.objects.filter(tenant=tenant).select_related("product"):
        skunow_totals.setdefault(b.product.sku, Decimal("0.00"))
        skunow_totals[b.product.sku] += (b.on_hand or Decimal("0.00"))

    rows = []
    all_skus = sorted(set(list(skunow_totals.keys()) + list(latest.keys())))
    for sku in all_skus:
        sk_qty = skunow_totals.get(sku, Decimal("0.00"))
        ch_qty = latest.get(sku).quantity if sku in latest else Decimal("0.00")
        drift = ch_qty - sk_qty
        rows.append({"sku": sku, "skunow_qty": sk_qty, "channel_qty": ch_qty, "drift": drift})

    return render(request, "reconcile.html", {"tenant": tenant, "rows": rows})

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_READONLY])

def product_list(request):
    tenant = _get_default_tenant(request)
    qs = Product.objects.filter(tenant=tenant).order_by("sku")
    return render(request, "products/product_list.html", {"tenant": tenant, "products": qs})

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])

def product_create(request):
    tenant = _get_default_tenant(request)
    if request.method == "POST":
        form = ProductForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.tenant = tenant
            try:
                obj.save()
                # Optional barcode
                barcode = form.cleaned_data.get("barcode")
                if barcode:
                    ProductBarcode.objects.get_or_create(tenant=tenant, code=barcode, defaults={"product": obj})
                messages.success(request, "Product created.")
                return redirect("product_list")
            except IntegrityError:
                form.add_error("sku", "SKU already exists for this tenant.")
    else:
        form = ProductForm()

    return render(request, "products/product_form.html", {
        "tenant": tenant, "form": form, "mode": "create"
    })

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])

def product_edit(request, product_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(Product, id=product_id, tenant=tenant)

    if request.method == "POST":
        form = ProductForm(request.POST, instance=obj)
        if form.is_valid():
            try:
                form.save()
                barcode = form.cleaned_data.get("barcode")
                if barcode:
                    ProductBarcode.objects.update_or_create(
                        tenant=tenant, code=barcode, defaults={"product": obj}
                    )
                messages.success(request, "Product updated.")
                return redirect("product_list")
            except IntegrityError:
                form.add_error("sku", "SKU already exists for this tenant.")
    else:
        initial = {}
        bc = obj.barcodes.order_by('id').first() if hasattr(obj, 'barcodes') else None
        if bc:
            initial['barcode'] = bc.code
        form = ProductForm(instance=obj, initial=initial)

    return render(request, "products/product_form.html", {
        "tenant": tenant, "form": form, "mode": "edit", "product": obj
    })

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])

def product_delete(request, product_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(Product, id=product_id, tenant=tenant)

    if request.method == "POST":
        obj.delete()
        messages.success(request, "Product deleted.")
        return redirect("product_list")

    return render(request, "products/product_delete.html", {
        "tenant": tenant, "product": obj
    })

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_READONLY])

def supplier_list(request):
    tenant = _get_default_tenant(request)
    suppliers = Supplier.objects.filter(tenant=tenant).order_by("name")
    return render(request, "suppliers/supplier_list.html", {"tenant": tenant, "suppliers": suppliers})

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])

def supplier_create(request):
    tenant = _get_default_tenant(request)
    form = SupplierForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        try:
            obj.save()
            messages.success(request, "Supplier created.")
            return redirect("supplier_list")
        except IntegrityError:
            form.add_error("name", "Supplier name already exists.")
    return render(request, "suppliers/supplier_form.html", {"tenant": tenant, "form": form, "mode": "create"})

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])

def supplier_edit(request, supplier_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(Supplier, id=supplier_id, tenant=tenant)
    form = SupplierForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
            messages.success(request, "Supplier updated.")
            return redirect("supplier_list")
        except IntegrityError:
            form.add_error("name", "Supplier name already exists.")
    return render(request, "suppliers/supplier_form.html", {"tenant": tenant, "form": form, "mode": "edit"})

@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])

def supplier_delete(request, supplier_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(Supplier, id=supplier_id, tenant=tenant)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Supplier deleted.")
        return redirect("supplier_list")
    return render(request, "suppliers/supplier_delete.html", {"tenant": tenant, "supplier": obj})

@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_READONLY])

def location_list(request):
    tenant = _get_default_tenant(request)
    locations = Location.objects.filter(tenant=tenant).order_by("name")
    return render(request, "locations/location_list.html", {"tenant": tenant, "locations": locations})

@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE], [ROLE_ADMIN, ROLE_WAREHOUSE])

def location_create(request):
    tenant = _get_default_tenant(request)
    form = LocationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        try:
            obj.save()
            messages.success(request, "Location created.")
            return redirect("location_list")
        except IntegrityError:
            form.add_error("name", "Location name already exists.")
    return render(request, "locations/location_form.html", {"tenant": tenant, "form": form, "mode": "create"})

@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE], [ROLE_ADMIN, ROLE_WAREHOUSE])

def location_edit(request, location_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(Location, id=location_id, tenant=tenant)
    form = LocationForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
            messages.success(request, "Location updated.")
            return redirect("location_list")
        except IntegrityError:
            form.add_error("name", "Location name already exists.")
    return render(request, "locations/location_form.html", {"tenant": tenant, "form": form, "mode": "edit"})

@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE], [ROLE_ADMIN, ROLE_WAREHOUSE])

def location_delete(request, location_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(Location, id=location_id, tenant=tenant)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Location deleted.")
        return redirect("location_list")
    return render(request, "locations/location_delete.html", {"tenant": tenant, "location": obj})

@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE])

def channel_list(request):
    tenant = _get_default_tenant(request)
    conns = ChannelConnection.objects.filter(tenant=tenant).order_by("channel", "name")
    return render(request, "channels/channel_list.html", {"tenant": tenant, "conns": conns})

@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE], [ROLE_ADMIN, ROLE_FINANCE])

def channel_create(request):
    tenant = _get_default_tenant(request)
    form = ChannelConnectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        obj.save()
        messages.success(request, "Connection saved.")
        return redirect("channel_list")
    return render(request, "channels/channel_form.html", {"tenant": tenant, "form": form, "mode": "create"})

@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE], [ROLE_ADMIN, ROLE_FINANCE])

def channel_edit(request, conn_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(ChannelConnection, id=conn_id, tenant=tenant)
    form = ChannelConnectionForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Connection updated.")
        return redirect("channel_list")
    return render(request, "channels/channel_form.html", {"tenant": tenant, "form": form, "mode": "edit"})

@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE], [ROLE_ADMIN, ROLE_FINANCE])

def channel_delete(request, conn_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(ChannelConnection, id=conn_id, tenant=tenant)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Connection deleted.")
        return redirect("channel_list")
    return render(request, "channels/channel_delete.html", {"tenant": tenant, "conn": obj})

@login_required
@role_required([ROLE_ADMIN, ROLE_SALES, ROLE_READONLY])

def sales_order_list(request):
    tenant = _get_default_tenant(request)
    orders = SalesOrder.objects.filter(tenant=tenant).order_by("-order_date")
    return render(request, "sales/sales_order_list.html", {"tenant": tenant, "orders": orders})

@transaction.atomic
@login_required
@role_required([ROLE_ADMIN, ROLE_SALES], [ROLE_ADMIN, ROLE_SALES])
def sales_order_create(request):
    tenant = _get_default_tenant(request)
    order = SalesOrder(tenant=tenant, currency_code=tenant.currency_code)

    if request.method == "POST":
        form = SalesOrderForm(request.POST, instance=order)
        formset = SalesOrderLineFormSet(request.POST, instance=order)
        if form.is_valid() and formset.is_valid():
            order = form.save(commit=False)
            order.tenant = tenant
            order.currency_code = tenant.currency_code
            order.save()
            formset.save()

            # Sync reservations for draft orders (components for kits)
            ref_id = f"{order.channel}:{order.order_number}"
            release_reservations(tenant=tenant, ref_type="SALES_ORDER", ref_id=ref_id)

            for line in order.lines.select_related("product").all():
                ship_loc = line.ship_from_location or order.ship_from_location
                if not ship_loc:
                    continue
                for comp, comp_qty in explode_product(line.product, Decimal(line.qty)):
                    reserve_stock(
                        tenant=tenant,
                        product=comp,
                        location=ship_loc,
                        qty=comp_qty,
                        ref_type="SALES_ORDER",
                        ref_id=ref_id,
                        lot_code=line.lot_code,
                        serial_number=line.serial_number,
                        expiry_date=line.expiry_date,
                    )

            action = form.cleaned_data.get("action") or "save"
            shortages = []
            if action == "post":
                shortages = _post_sales_order(order) or []
                if shortages:
                    sample = ", ".join([f"{s['sku']}@{s['location']} short {s['short_by']}" for s in shortages[:6]])
                    more = "" if len(shortages) <= 6 else f" (+{len(shortages)-6} more)"
                    messages.warning(request, "Posted with shortages (allowed): " + sample + more)
                else:
                    messages.success(request, "Sales order posted.")

            return redirect("sales_order_detail", order_id=order.id)
    else:
        form = SalesOrderForm(instance=order)
        formset = SalesOrderLineFormSet(instance=order)

    return render(request, "sales/sales_order_form.html", {
        "tenant": tenant, "form": form, "formset": formset, "mode": "create"
    })


@login_required
@role_required([ROLE_ADMIN, ROLE_SALES, ROLE_READONLY])

def sales_order_detail(request, order_id):
    tenant = _get_default_tenant(request)
    order = get_object_or_404(SalesOrder, id=order_id, tenant=tenant)
    subtotal = sum((l.line_total for l in order.lines.all()), Decimal("0.00"))
    return render(request, "sales/sales_order_detail.html", {
        "tenant": tenant, "order": order, "subtotal": subtotal
    })

@transaction.atomic
@login_required
@role_required([ROLE_ADMIN, ROLE_SALES], [ROLE_ADMIN, ROLE_SALES])
def sales_order_post(request, order_id):
    tenant = _get_default_tenant(request)
    order = get_object_or_404(SalesOrder, id=order_id, tenant=tenant)
    if request.method == "POST":
        shortages = _post_sales_order(order) or []
        if shortages:
            sample = ", ".join([f"{s['sku']}@{s['location']} short {s['short_by']}" for s in shortages[:6]])
            more = "" if len(shortages) <= 6 else f" (+{len(shortages)-6} more)"
            messages.warning(request, "Posted with shortages (allowed): " + sample + more)
        else:
            messages.success(request, "Sales order posted.")
        return redirect("sales_order_detail", order_id=order.id)
    return render(request, "sales/sales_order_post.html", {"tenant": tenant, "order": order})

def _post_sales_order(order: SalesOrder):
    """Post sales order:
    - releases existing reservations for the order
    - deducts inventory via movements (kit policy: deduct components only)
    - allows negative inventory but returns shortages for UI warnings (per your rule)
    """
    if order.status == SalesOrder.Status.POSTED:
        return []

    shortages = []
    ref_id = f"{order.channel}:{order.order_number}"

    # Release reserved qty first (we still post even if insufficient; warn)
    release_reservations(tenant=order.tenant, ref_type="SALES_ORDER", ref_id=ref_id)

    cogs_total = Decimal("0.00")
    for line in order.lines.select_related("product").all():
        ship_loc = line.ship_from_location or order.ship_from_location
        if not ship_loc:
            continue

        qty = Decimal(line.qty)
        if qty <= 0:
            continue

        # Explode kits/bundles to components (recommended ERP approach)
        for comp, comp_qty in explode_product(line.product, qty):
            if comp_qty <= 0:
                continue

            # availability check (allow with warning)
            try:
                bal = InventoryBalance.objects.get(tenant=order.tenant, product=comp, location=ship_loc)
                available = (bal.on_hand or Decimal("0.00")) - (bal.reserved or Decimal("0.00"))
            except InventoryBalance.DoesNotExist:
                available = Decimal("0.00")

            if available < comp_qty:
                shortages.append({
                    "sku": comp.sku,
                    "location": ship_loc.name,
                    "required": str(comp_qty),
                    "available": str(available),
                    "short_by": str(comp_qty - available),
                })

            movement = apply_movement(
                tenant=order.tenant,
                product=comp,
                location=ship_loc,
                movement_type=InventoryMovement.MovementType.SALE,
                qty_delta=(comp_qty * Decimal("-1")),
                ref_type="SALES_ORDER",
                ref_id=ref_id,
                notes="Sales order posted",
                lot_code=line.lot_code,
                serial_number=line.serial_number,
                expiry_date=line.expiry_date,
            )
            # movement.value is negative for outbound; COGS is its absolute value.
            cogs_total += -(movement.value or Decimal("0.00"))

    # Expense cost of goods sold: DR COGS / CR Inventory.
    post_cogs(order.tenant, cogs_total, ref_id, entry_date=order.order_date.date())

    order.status = SalesOrder.Status.POSTED
    order.save()
    return shortages

@login_required
@role_required([ROLE_ADMIN])

def settings_tenant(request):
    tenant = _get_default_tenant(request)
    if not tenant:
        # your app already uses needs_setup state
        return redirect("/admin/")

    form = TenantSettingsForm(request.POST or None, instance=tenant)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings updated.")
        return redirect("settings_tenant")

    return render(request, "settings/tenant_settings.html", {
        "tenant": tenant,
        "form": form
    })

@login_required
@role_required([ROLE_ADMIN], [ROLE_ADMIN])
def uom_list(request):
    tenant = _get_default_tenant(request)
    uoms = UnitOfMeasure.objects.filter(tenant=tenant).order_by("code")
    return render(request, "uoms/uom_list.html", {"tenant": tenant, "uoms": uoms})


@login_required
@role_required([ROLE_ADMIN], [ROLE_ADMIN])
def uom_create(request):
    tenant = _get_default_tenant(request)
    form = UnitOfMeasureForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        try:
            obj.save()
            messages.success(request, "UOM created.")
            return redirect("uom_list")
        except IntegrityError:
            form.add_error("code", "UOM code already exists.")
    return render(request, "uoms/uom_form.html", {"tenant": tenant, "form": form, "mode": "create"})


@login_required
@role_required([ROLE_ADMIN], [ROLE_ADMIN])
def uom_edit(request, uom_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(UnitOfMeasure, id=uom_id, tenant=tenant)
    form = UnitOfMeasureForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
            messages.success(request, "UOM updated.")
            return redirect("uom_list")
        except IntegrityError:
            form.add_error("code", "UOM code already exists.")
    return render(request, "uoms/uom_form.html", {"tenant": tenant, "form": form, "mode": "edit"})


@login_required
@role_required([ROLE_ADMIN], [ROLE_ADMIN])
def uom_delete(request, uom_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(UnitOfMeasure, id=uom_id, tenant=tenant)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "UOM deleted.")
        return redirect("uom_list")
    return render(request, "uoms/uom_delete.html", {"tenant": tenant, "uom": obj})


@login_required
@role_required([ROLE_ADMIN], [ROLE_ADMIN])
def uom_conversion_list(request):
    tenant = _get_default_tenant(request)
    conversions = UOMConversion.objects.filter(tenant=tenant).select_related("product", "from_uom", "to_uom").order_by("product__sku", "from_uom__code")
    return render(request, "uoms/conversion_list.html", {"tenant": tenant, "conversions": conversions})


@login_required
@role_required([ROLE_ADMIN], [ROLE_ADMIN])
def uom_conversion_create(request):
    tenant = _get_default_tenant(request)
    form = UOMConversionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        try:
            obj.save()
            messages.success(request, "Conversion saved.")
            return redirect("uom_conversion_list")
        except IntegrityError:
            form.add_error(None, "This conversion already exists.")
    return render(request, "uoms/conversion_form.html", {"tenant": tenant, "form": form, "mode": "create"})


@login_required
@role_required([ROLE_ADMIN], [ROLE_ADMIN])
def uom_conversion_edit(request, conv_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(UOMConversion, id=conv_id, tenant=tenant)
    form = UOMConversionForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
            messages.success(request, "Conversion updated.")
            return redirect("uom_conversion_list")
        except IntegrityError:
            form.add_error(None, "This conversion already exists.")
    return render(request, "uoms/conversion_form.html", {"tenant": tenant, "form": form, "mode": "edit"})


@login_required
@role_required([ROLE_ADMIN], [ROLE_ADMIN])
def uom_conversion_delete(request, conv_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(UOMConversion, id=conv_id, tenant=tenant)
    if request.method == "POST":
        obj.delete()
        messages.success(request, "Conversion deleted.")
        return redirect("uom_conversion_list")
    return render(request, "uoms/conversion_delete.html", {"tenant": tenant, "conv": obj})


@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_SALES], [ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_SALES])
def bom_list(request):
    tenant = _get_default_tenant(request)
    boms = BillOfMaterials.objects.filter(tenant=tenant).select_related("product").order_by("-is_active", "product__sku")
    return render(request, "boms/bom_list.html", {"tenant": tenant, "boms": boms})


@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])
def bom_create(request):
    tenant = _get_default_tenant(request)
    form = BillOfMaterialsForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        obj.save()
        messages.success(request, "BOM created.")
        return redirect("bom_detail", bom_id=obj.id)
    return render(request, "boms/bom_form.html", {"tenant": tenant, "form": form, "mode": "create"})


@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_SALES], [ROLE_ADMIN, ROLE_PROCUREMENT, ROLE_SALES])
def bom_detail(request, bom_id):
    tenant = _get_default_tenant(request)
    bom = get_object_or_404(BillOfMaterials, id=bom_id, tenant=tenant)
    if request.method == "POST":
        form = BillOfMaterialsForm(request.POST, instance=bom)
        formset = BOMLineFormSet(request.POST, instance=bom)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, "BOM updated.")
            return redirect("bom_detail", bom_id=bom.id)
    else:
        form = BillOfMaterialsForm(instance=bom)
        formset = BOMLineFormSet(instance=bom)
    return render(request, "boms/bom_detail.html", {"tenant": tenant, "bom": bom, "form": form, "formset": formset})


@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])
def bom_delete(request, bom_id):
    tenant = _get_default_tenant(request)
    bom = get_object_or_404(BillOfMaterials, id=bom_id, tenant=tenant)
    if request.method == "POST":
        bom.delete()
        messages.success(request, "BOM deleted.")
        return redirect("bom_list")
    return render(request, "boms/bom_delete.html", {"tenant": tenant, "bom": bom})


# ---------------- Cycle Counts ----------------

from core.auth import role_required, ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_FINANCE
from core.forms import CycleCountForm, CycleCountLineFormSet
from core.models import CycleCount, CycleCountLine, InventoryLotBalance

@role_required(read_groups=[ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_FINANCE])
def cycle_count_list(request):
    tenant = _get_default_tenant(request)
    qs = CycleCount.objects.filter(tenant=tenant).select_related("location").order_by("-created_at")
    return render(request, "inventory/cycle_count_list.html", {"tenant": tenant, "cycle_counts": qs})

@role_required(read_groups=[ROLE_ADMIN, ROLE_WAREHOUSE], write_groups=[ROLE_ADMIN, ROLE_WAREHOUSE])
@transaction.atomic
def cycle_count_create(request):
    tenant = _get_default_tenant(request)
    cc = CycleCount(tenant=tenant)
    if request.method == "POST":
        form = CycleCountForm(request.POST, instance=cc)
        formset = CycleCountLineFormSet(request.POST, instance=cc)
        if form.is_valid() and formset.is_valid():
            cc = form.save(commit=False)
            cc.tenant = tenant
            cc.save()
            formset.save()
            return redirect("cycle_count_detail", cc_id=cc.id)
    else:
        form = CycleCountForm(instance=cc)
        formset = CycleCountLineFormSet(instance=cc)

    return render(request, "inventory/cycle_count_form.html", {
        "tenant": tenant, "form": form, "formset": formset
    })

@role_required(read_groups=[ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_FINANCE])
def cycle_count_detail(request, cc_id):
    tenant = _get_default_tenant(request)
    cc = get_object_or_404(CycleCount, id=cc_id, tenant=tenant)
    return render(request, "inventory/cycle_count_detail.html", {"tenant": tenant, "cc": cc})

@role_required(read_groups=[ROLE_ADMIN, ROLE_WAREHOUSE], write_groups=[ROLE_ADMIN, ROLE_WAREHOUSE])
@transaction.atomic
def cycle_count_submit(request, cc_id):
    tenant = _get_default_tenant(request)
    cc = get_object_or_404(CycleCount, id=cc_id, tenant=tenant)
    if request.method != "POST":
        return redirect("cycle_count_detail", cc_id=cc.id)

    if cc.status != CycleCount.Status.DRAFT:
        return redirect("cycle_count_detail", cc_id=cc.id)

    # Snapshot system qty + calculate variance
    for line in cc.lines.select_related("product").all():
        if line.lot_code or line.serial_number or line.expiry_date:
            try:
                lb = InventoryLotBalance.objects.get(
                    tenant=tenant, product=line.product, location=cc.location,
                    lot_code=line.lot_code, serial_number=line.serial_number, expiry_date=line.expiry_date
                )
                system_qty = lb.on_hand
            except InventoryLotBalance.DoesNotExist:
                system_qty = Decimal("0.00")
        else:
            try:
                bal = InventoryBalance.objects.get(tenant=tenant, product=line.product, location=cc.location)
                system_qty = bal.on_hand
            except InventoryBalance.DoesNotExist:
                system_qty = Decimal("0.00")

        line.system_qty = system_qty
        line.variance_qty = Decimal(line.counted_qty) - system_qty
        line.save()

    cc.status = CycleCount.Status.SUBMITTED
    cc.save()
    return redirect("cycle_count_detail", cc_id=cc.id)

@role_required(read_groups=[ROLE_ADMIN, ROLE_FINANCE], write_groups=[ROLE_ADMIN, ROLE_FINANCE])
@transaction.atomic
def cycle_count_approve(request, cc_id):
    tenant = _get_default_tenant(request)
    cc = get_object_or_404(CycleCount, id=cc_id, tenant=tenant)
    if request.method == "POST" and cc.status == CycleCount.Status.SUBMITTED:
        cc.status = CycleCount.Status.APPROVED
        cc.save()
    return redirect("cycle_count_detail", cc_id=cc.id)

@role_required(read_groups=[ROLE_ADMIN, ROLE_FINANCE], write_groups=[ROLE_ADMIN, ROLE_FINANCE])
@transaction.atomic
def cycle_count_post(request, cc_id):
    tenant = _get_default_tenant(request)
    cc = get_object_or_404(CycleCount, id=cc_id, tenant=tenant)
    if request.method != "POST":
        return render(request, "inventory/cycle_count_post.html", {"tenant": tenant, "cc": cc})

    if cc.status != CycleCount.Status.APPROVED:
        return redirect("cycle_count_detail", cc_id=cc.id)

    # Post variances as ADJUSTMENT movements
    for line in cc.lines.select_related("product").all():
        if Decimal(line.variance_qty) == Decimal("0.00"):
            continue
        apply_movement(
            tenant=tenant,
            product=line.product,
            location=cc.location,
            movement_type="ADJUSTMENT",
            qty_delta=Decimal(line.variance_qty),
            ref_type="CYCLE_COUNT",
            ref_id=str(cc.id),
            notes="Cycle count variance posted",
            lot_code=line.lot_code,
            serial_number=line.serial_number,
            expiry_date=line.expiry_date
        )

    cc.status = CycleCount.Status.POSTED
    cc.save()
    return redirect("cycle_count_detail", cc_id=cc.id)


@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE])
def transfer_list(request):
    tenant = _get_default_tenant(request)
    transfers = InventoryTransfer.objects.filter(tenant=tenant).order_by("-created_at")
    return render(request, "transfers/transfer_list.html", {"tenant": tenant, "transfers": transfers})


@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE])
@transaction.atomic
def transfer_create(request):
    tenant = _get_default_tenant(request)
    transfer = InventoryTransfer(tenant=tenant, transfer_number="TR-" + timezone.now().strftime("%Y%m%d-%H%M%S-%f"))
    if request.method == "POST":
        form = InventoryTransferForm(request.POST, instance=transfer)
        formset = InventoryTransferLineFormSet(request.POST, instance=transfer)
        if form.is_valid() and formset.is_valid():
            transfer = form.save(commit=False)
            transfer.tenant = tenant
            transfer.save()
            formset.save()

            action = form.cleaned_data.get("action") or "save"
            if action == "post":
                _post_transfer(transfer, request)
            return redirect("transfer_detail", transfer_id=transfer.id)
    else:
        form = InventoryTransferForm(instance=transfer)
        formset = InventoryTransferLineFormSet(instance=transfer)

    return render(request, "transfers/transfer_form.html", {
        "tenant": tenant, "form": form, "formset": formset, "mode": "create"
    })


@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE])
def transfer_detail(request, transfer_id):
    tenant = _get_default_tenant(request)
    transfer = get_object_or_404(InventoryTransfer, id=transfer_id, tenant=tenant)
    return render(request, "transfers/transfer_detail.html", {"tenant": tenant, "transfer": transfer})


@login_required
@role_required([ROLE_ADMIN, ROLE_WAREHOUSE])
@transaction.atomic
def transfer_post(request, transfer_id):
    tenant = _get_default_tenant(request)
    transfer = get_object_or_404(InventoryTransfer, id=transfer_id, tenant=tenant)
    if request.method == "POST":
        _post_transfer(transfer, request)
        return redirect("transfer_detail", transfer_id=transfer.id)
    return render(request, "transfers/transfer_post.html", {"tenant": tenant, "transfer": transfer})


def _post_transfer(transfer: InventoryTransfer, request=None):
    if transfer.status == InventoryTransfer.Status.POSTED:
        return
    # Post OUT then IN (lot-aware)
    for line in transfer.lines.select_related("product").all():
        qty = Decimal(line.qty)
        if qty <= 0:
            continue
        apply_movement(
            tenant=transfer.tenant,
            product=line.product,
            location=transfer.from_location,
            movement_type="TRANSFER_OUT",
            qty_delta=(qty * Decimal("-1")),
            ref_type="TRANSFER",
            ref_id=transfer.transfer_number,
            notes="Transfer out",
            lot_code=line.lot_code,
            serial_number=line.serial_number,
            expiry_date=line.expiry_date,
        )
        apply_movement(
            tenant=transfer.tenant,
            product=line.product,
            location=transfer.to_location,
            movement_type="TRANSFER_IN",
            qty_delta=qty,
            ref_type="TRANSFER",
            ref_id=transfer.transfer_number,
            notes="Transfer in",
            lot_code=line.lot_code,
            serial_number=line.serial_number,
            expiry_date=line.expiry_date,
        )
    transfer.status = InventoryTransfer.Status.POSTED
    transfer.posted_at = timezone.now()
    transfer.save()
    if request is not None:
        messages.success(request, f"Transfer {transfer.transfer_number} posted.")


@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE])
def invoice_list(request):
    tenant = _get_default_tenant(request)
    invoices = SupplierInvoice.objects.filter(tenant=tenant).order_by("-created_at")
    return render(request, "finance/invoice_list.html", {"tenant": tenant, "invoices": invoices})


@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE])
@transaction.atomic
def invoice_create(request):
    tenant = _get_default_tenant(request)
    inv = SupplierInvoice(tenant=tenant, currency_code=tenant.currency_code)
    if request.method == "POST":
        form = SupplierInvoiceForm(request.POST, request.FILES, instance=inv)
        formset = SupplierInvoiceLineFormSet(request.POST, instance=inv)
        if form.is_valid() and formset.is_valid():
            inv = form.save(commit=False)
            inv.tenant = tenant
            inv.save()
            formset.save()

            action = form.cleaned_data.get("action") or "save"
            if action == "submit":
                _run_3way_match(inv, request)
            elif action == "approve":
                inv.status = SupplierInvoice.Status.APPROVED
                inv.approved_by = request.user
                inv.approved_at = timezone.now()
                inv.save()
                messages.success(request, "Invoice approved.")
            elif action == "post":
                _run_3way_match(inv, request)
                inv.status = SupplierInvoice.Status.POSTED
                inv.save()
                messages.success(request, "Invoice posted.")
            return redirect("invoice_detail", invoice_id=inv.id)
    else:
        form = SupplierInvoiceForm(instance=inv)
        formset = SupplierInvoiceLineFormSet(instance=inv)

    return render(request, "finance/invoice_form.html", {"tenant": tenant, "form": form, "formset": formset})


@login_required
@role_required([ROLE_ADMIN, ROLE_FINANCE])
def invoice_detail(request, invoice_id):
    tenant = _get_default_tenant(request)
    inv = get_object_or_404(SupplierInvoice, id=invoice_id, tenant=tenant)
    match = _compute_3way(inv)
    return render(request, "finance/invoice_detail.html", {"tenant": tenant, "invoice": inv, "match": match})


def _compute_3way(inv: SupplierInvoice):
    # Return discrepancies list for UI
    discrepancies = []
    for line in inv.lines.select_related("product","po_line","receipt_line").all():
        po_qty = line.po_line.ordered_qty if line.po_line else None
        rec_qty = line.receipt_line.qty_received if line.receipt_line else None
        if rec_qty is not None and line.qty > rec_qty:
            discrepancies.append(f"{line.product.sku}: invoiced qty {line.qty} > received {rec_qty}")
        if po_qty is not None and line.qty > po_qty:
            discrepancies.append(f"{line.product.sku}: invoiced qty {line.qty} > ordered {po_qty}")
    return {"ok": len(discrepancies)==0, "discrepancies": discrepancies}


def _run_3way_match(inv: SupplierInvoice, request=None):
    m = _compute_3way(inv)
    if m["ok"]:
        inv.status = SupplierInvoice.Status.MATCHED
        inv.save()
        if request is not None:
            messages.success(request, "3-way match passed. Invoice marked MATCHED.")
    else:
        inv.status = SupplierInvoice.Status.DRAFT
        inv.save()
        if request is not None:
            messages.warning(request, "3-way match issues: " + "; ".join(m["discrepancies"][:5]))


@login_required
@role_required([ROLE_ADMIN, ROLE_SALES, ROLE_WAREHOUSE, ROLE_READONLY])
def return_list(request):
    tenant = _get_default_tenant(request)
    rmas = ReturnAuthorization.objects.filter(tenant=tenant).order_by("-created_at")
    return render(request, "returns/return_list.html", {"tenant": tenant, "rmas": rmas})

@login_required
@role_required([ROLE_ADMIN, ROLE_SALES, ROLE_WAREHOUSE], [ROLE_ADMIN, ROLE_SALES, ROLE_WAREHOUSE])
@transaction.atomic
def return_create(request):
    tenant = _get_default_tenant(request)
    rma = ReturnAuthorization(tenant=tenant)

    if request.method == "POST":
        form = ReturnAuthorizationForm(request.POST, instance=rma)
        formset = ReturnLineFormSet(request.POST, instance=rma)
        if form.is_valid() and formset.is_valid():
            rma = form.save(commit=False)
            rma.tenant = tenant
            rma.save()
            formset.save()

            action = form.cleaned_data.get("action") or "save"
            if action == "approve" and rma.status == ReturnAuthorization.Status.DRAFT:
                rma.status = ReturnAuthorization.Status.APPROVED
                rma.save()
                messages.success(request, "RMA approved.")
            elif action == "receive":
                _receive_rma(rma)
                messages.success(request, "Return received and restocked.")
            return redirect("return_detail", rma_id=rma.id)
    else:
        form = ReturnAuthorizationForm(instance=rma)
        formset = ReturnLineFormSet(instance=rma)

    return render(request, "returns/return_form.html", {
        "tenant": tenant, "form": form, "formset": formset, "mode": "create"
    })

@login_required
@role_required([ROLE_ADMIN, ROLE_SALES, ROLE_WAREHOUSE, ROLE_READONLY])
def return_detail(request, rma_id):
    tenant = _get_default_tenant(request)
    rma = get_object_or_404(ReturnAuthorization, id=rma_id, tenant=tenant)
    return render(request, "returns/return_detail.html", {"tenant": tenant, "rma": rma})

@login_required
@role_required([ROLE_ADMIN, ROLE_SALES, ROLE_WAREHOUSE], [ROLE_ADMIN, ROLE_SALES, ROLE_WAREHOUSE])
@transaction.atomic
def return_process(request, rma_id):
    tenant = _get_default_tenant(request)
    rma = get_object_or_404(ReturnAuthorization, id=rma_id, tenant=tenant)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve" and rma.status == ReturnAuthorization.Status.DRAFT:
            rma.status = ReturnAuthorization.Status.APPROVED
            rma.save()
            messages.success(request, "RMA approved.")
        elif action == "receive":
            _receive_rma(rma)
            messages.success(request, "Return received and restocked.")
        return redirect("return_detail", rma_id=rma.id)
    return redirect("return_detail", rma_id=rma.id)

def _receive_rma(rma: ReturnAuthorization):
    if rma.status in (ReturnAuthorization.Status.RECEIVED, ReturnAuthorization.Status.CLOSED):
        return
    # allow receiving from DRAFT/APPROVED
    ref_id = f"{rma.channel}:{rma.rma_number}"
    for line in rma.lines.select_related("product").all():
        qty = Decimal(line.qty)
        if qty <= 0:
            continue
        # If returned item is a kit, we restock components (consistent with 'deduct components only')
        for comp, comp_qty in explode_product(line.product, qty):
            apply_movement(
                tenant=rma.tenant,
                product=comp,
                location=rma.receive_location,
                movement_type=InventoryMovement.MovementType.RETURN,
                qty_delta=comp_qty,
                ref_type="RMA",
                ref_id=ref_id,
                notes="Return received",
                lot_code=line.lot_code,
                serial_number=line.serial_number,
                expiry_date=line.expiry_date,
            )
    rma.status = ReturnAuthorization.Status.RECEIVED
    rma.save()


# ============================
# VAT / Tax Codes
# ============================

@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def taxcode_list(request):
    tenant = _get_default_tenant(request)
    codes = TaxCode.objects.filter(tenant=tenant).order_by("code")
    return render(request, "tax/taxcode_list.html", {"tenant": tenant, "codes": codes})

@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def taxcode_create(request):
    tenant = _get_default_tenant(request)
    form = TaxCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        obj.save()
        return redirect("taxcode_list")
    return render(request, "tax/taxcode_form.html", {"tenant": tenant, "form": form, "mode": "create"})

@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def taxcode_edit(request, tax_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(TaxCode, id=tax_id, tenant=tenant)
    form = TaxCodeForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("taxcode_list")
    return render(request, "tax/taxcode_form.html", {"tenant": tenant, "form": form, "mode": "edit"})

@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def taxcode_delete(request, tax_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(TaxCode, id=tax_id, tenant=tenant)
    if request.method == "POST":
        obj.delete()
        return redirect("taxcode_list")
    return render(request, "tax/taxcode_delete.html", {"tenant": tenant, "tax": obj})


# ============================
# Customers
# ============================

@role_required([ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN])
def customer_list(request):
    tenant = _get_default_tenant(request)
    customers = Customer.objects.filter(tenant=tenant).order_by("name")
    return render(request, "customers/customer_list.html", {"tenant": tenant, "customers": customers})

@role_required([ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN])
def customer_create(request):
    tenant = _get_default_tenant(request)
    form = CustomerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        obj.save()
        return redirect("customer_list")
    return render(request, "customers/customer_form.html", {"tenant": tenant, "form": form, "mode": "create"})

@role_required([ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN])
def customer_edit(request, customer_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(Customer, id=customer_id, tenant=tenant)
    form = CustomerForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("customer_list")
    return render(request, "customers/customer_form.html", {"tenant": tenant, "form": form, "mode": "edit"})


# ============================
# Accounts Receivable Invoices
# ============================

@role_required([ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN])
def ar_invoice_list(request):
    tenant = _get_default_tenant(request)
    invoices = (
        CustomerInvoice.objects.filter(tenant=tenant)
        .prefetch_related("lines", "lines__tax_code")
        .order_by("-invoice_date", "-id")
    )
    return render(request, "ar/ar_invoice_list.html", {"tenant": tenant, "invoices": invoices})

@role_required([ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN])
@transaction.atomic
def ar_invoice_create(request):
    tenant = _get_default_tenant(request)
    inv = CustomerInvoice(tenant=tenant, currency_code=tenant.currency_code)
    if request.method == "POST":
        form = CustomerInvoiceForm(request.POST, instance=inv)
        formset = CustomerInvoiceLineFormSet(request.POST, instance=inv)
        if form.is_valid() and formset.is_valid():
            inv = form.save(commit=False)
            inv.tenant = tenant
            inv.currency_code = tenant.currency_code
            inv.save()
            formset.save()

            action = form.cleaned_data.get("action") or "save"
            if action == "issue":
                post_customer_invoice(inv, user=request.user)

            return redirect("ar_invoice_detail", invoice_id=inv.id)
    else:
        form = CustomerInvoiceForm(instance=inv)
        formset = CustomerInvoiceLineFormSet(instance=inv)

    return render(request, "ar/ar_invoice_form.html", {"tenant": tenant, "form": form, "formset": formset})

@role_required([ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN])
def ar_invoice_detail(request, invoice_id):
    tenant = _get_default_tenant(request)
    inv = get_object_or_404(CustomerInvoice, id=invoice_id, tenant=tenant)
    return render(request, "ar/ar_invoice_detail.html", {"tenant": tenant, "inv": inv})

@role_required([ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_SALES, ROLE_FINANCE, ROLE_ADMIN])
@transaction.atomic
def ar_invoice_issue(request, invoice_id):
    tenant = _get_default_tenant(request)
    inv = get_object_or_404(CustomerInvoice, id=invoice_id, tenant=tenant)
    if request.method == "POST":
        post_customer_invoice(inv, user=request.user)
        return redirect("ar_invoice_detail", invoice_id=inv.id)
    return render(request, "ar/ar_invoice_issue.html", {"tenant": tenant, "inv": inv})


# ============================
# General Ledger
# ============================

@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def gl_account_list(request):
    tenant = _get_default_tenant(request)
    accounts = GLAccount.objects.filter(tenant=tenant).order_by("code")
    return render(request, "gl/gl_account_list.html", {"tenant": tenant, "accounts": accounts})

@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def gl_account_create(request):
    tenant = _get_default_tenant(request)
    form = GLAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.tenant = tenant
        obj.save()
        return redirect("gl_account_list")
    return render(request, "gl/gl_account_form.html", {"tenant": tenant, "form": form, "mode": "create"})

@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def gl_account_edit(request, account_id):
    tenant = _get_default_tenant(request)
    obj = get_object_or_404(GLAccount, id=account_id, tenant=tenant)
    form = GLAccountForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("gl_account_list")
    return render(request, "gl/gl_account_form.html", {"tenant": tenant, "form": form, "mode": "edit"})

@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def journal_list(request):
    tenant = _get_default_tenant(request)
    entries = JournalEntry.objects.filter(tenant=tenant).order_by("-entry_date", "-id")[:200]
    return render(request, "gl/journal_list.html", {"tenant": tenant, "entries": entries})

@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def journal_detail(request, je_id):
    tenant = _get_default_tenant(request)
    je = get_object_or_404(JournalEntry, id=je_id, tenant=tenant)
    return render(request, "gl/journal_detail.html", {"tenant": tenant, "je": je})


# ============================
# AP invoice posting (3-way match -> post to GL)
# ============================

@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
@transaction.atomic
def invoice_post(request, invoice_id):
    tenant = _get_default_tenant(request)
    inv = get_object_or_404(SupplierInvoice, id=invoice_id, tenant=tenant)
    if request.method == "POST":
        post_supplier_invoice(inv, user=request.user)
        return redirect("invoice_detail", invoice_id=inv.id)
    return render(request, "finance/invoice_post.html", {"tenant": tenant, "inv": inv})


@login_required
@role_required([ROLE_ADMIN, ROLE_PROCUREMENT], [ROLE_ADMIN, ROLE_PROCUREMENT])
def shipment_new(request, po_id):
    tenant = _get_default_tenant(request)
    po = get_object_or_404(PurchaseOrder, id=po_id, tenant=tenant)

    if po.status == PurchaseOrder.Status.DRAFT:
        messages.error(request, "Submit the PO before creating shipments.")
        return redirect("po_detail", po_id=po.id)

    dests = Location.objects.filter(tenant=tenant).order_by("name")
    if request.method == "POST":
        dest_id = request.POST.get("destination_id")
        dest = get_object_or_404(Location, id=dest_id, tenant=tenant)
        shipment = Shipment.objects.create(
            tenant=tenant,
            po=po,
            from_supplier=po.supplier,
            destination=dest,
            carrier=(request.POST.get("carrier") or "").strip() or None,
            tracking_number=(request.POST.get("tracking_number") or "").strip() or None,
            eta=(request.POST.get("eta") or None),
            status=Shipment.Status.CREATED,
        )
        # Create empty lines for this shipment (expected 0 by default)
        for pol in po.lines.all():
            ShipmentLine.objects.get_or_create(
                shipment=shipment,
                po_line=pol,
                defaults={"expected_qty": Decimal("0.00")},
            )
        messages.success(request, "Shipment created. Allocate quantities to it.")
        return redirect("shipment_detail", shipment_id=shipment.id)

    return render(request, "shipments/shipment_new.html", {"tenant": tenant, "po": po, "dests": dests})


# ============================
# Financial Reports
# ============================

def _parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return timezone.datetime.fromisoformat(raw).date()
    except (ValueError, TypeError):
        return None


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def reports_index(request):
    tenant = _get_default_tenant(request)
    return render(request, "reports/index.html", {"tenant": tenant})


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def report_trial_balance(request):
    tenant = _get_default_tenant(request)
    as_of = _parse_date(request.GET.get("as_of")) or timezone.localdate()
    data = reports_service.trial_balance(tenant, date_to=as_of)
    return render(request, "reports/trial_balance.html", {"tenant": tenant, "as_of": as_of, "data": data})


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def report_pnl(request):
    tenant = _get_default_tenant(request)
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to")) or timezone.localdate()
    data = reports_service.profit_and_loss(tenant, date_from=date_from, date_to=date_to)
    return render(request, "reports/pnl.html", {
        "tenant": tenant, "date_from": date_from, "date_to": date_to, "data": data,
    })


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def report_balance_sheet(request):
    tenant = _get_default_tenant(request)
    as_of = _parse_date(request.GET.get("as_of")) or timezone.localdate()
    data = reports_service.balance_sheet(tenant, as_of=as_of)
    return render(request, "reports/balance_sheet.html", {"tenant": tenant, "as_of": as_of, "data": data})


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def report_aged_receivables(request):
    tenant = _get_default_tenant(request)
    as_of = _parse_date(request.GET.get("as_of")) or timezone.localdate()
    data = reports_service.aged_receivables(tenant, as_of=as_of)
    return render(request, "reports/aged.html", {
        "tenant": tenant, "as_of": as_of, "data": data,
        "title": "Aged Debtors (Receivables)", "party_label": "Customer",
    })


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_WAREHOUSE, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def report_stock_valuation(request):
    tenant = _get_default_tenant(request)
    data = reports_service.stock_valuation(tenant)
    return render(request, "reports/stock_valuation.html", {"tenant": tenant, "data": data})


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def report_aged_payables(request):
    tenant = _get_default_tenant(request)
    as_of = _parse_date(request.GET.get("as_of")) or timezone.localdate()
    data = reports_service.aged_payables(tenant, as_of=as_of)
    return render(request, "reports/aged.html", {
        "tenant": tenant, "as_of": as_of, "data": data,
        "title": "Aged Creditors (Payables)", "party_label": "Supplier",
    })


# ============================
# Payments (AR receipts / AP payments) + bank reconciliation
# ============================

def _allocate_fifo(payment, open_invoices, invoice_field):
    """Allocate the payment amount across open invoices oldest-first."""
    remaining = payment.amount
    for inv in open_invoices:
        if remaining <= Decimal("0.00"):
            break
        outstanding = inv.outstanding
        if outstanding <= Decimal("0.00"):
            continue
        alloc = min(remaining, outstanding)
        PaymentAllocation.objects.create(payment=payment, amount=alloc, **{invoice_field: inv})
        remaining -= alloc


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def payment_list(request):
    tenant = _get_default_tenant(request)
    payments = Payment.objects.filter(tenant=tenant).select_related("customer", "supplier").order_by("-payment_date", "-id")
    return render(request, "payments/payment_list.html", {"tenant": tenant, "payments": payments})


@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
@transaction.atomic
def receipt_create(request):
    tenant = _get_default_tenant(request)
    form = ReceiptForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        payment = form.save(commit=False)
        payment.tenant = tenant
        payment.direction = Payment.Direction.RECEIPT
        payment.currency_code = tenant.currency_code
        payment.save()
        open_invoices = (CustomerInvoice.objects
                         .filter(tenant=tenant, customer=payment.customer, status=CustomerInvoice.Status.ISSUED)
                         .order_by("invoice_date", "id"))
        _allocate_fifo(payment, open_invoices, "customer_invoice")
        post_payment(payment, user=request.user)
        messages.success(request, f"Receipt of {payment.amount} recorded and allocated.")
        return redirect("payment_detail", payment_id=payment.id)
    return render(request, "payments/payment_form.html", {"tenant": tenant, "form": form, "mode": "receipt"})


@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
@transaction.atomic
def supplier_payment_create(request):
    tenant = _get_default_tenant(request)
    form = SupplierPaymentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        payment = form.save(commit=False)
        payment.tenant = tenant
        payment.direction = Payment.Direction.PAYMENT
        payment.currency_code = tenant.currency_code
        payment.save()
        open_invoices = (SupplierInvoice.objects
                         .filter(tenant=tenant, supplier=payment.supplier, status=SupplierInvoice.Status.POSTED)
                         .order_by("invoice_date", "id"))
        _allocate_fifo(payment, open_invoices, "supplier_invoice")
        post_payment(payment, user=request.user)
        messages.success(request, f"Payment of {payment.amount} recorded and allocated.")
        return redirect("payment_detail", payment_id=payment.id)
    return render(request, "payments/payment_form.html", {"tenant": tenant, "form": form, "mode": "payment"})


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def payment_detail(request, payment_id):
    tenant = _get_default_tenant(request)
    payment = get_object_or_404(Payment, id=payment_id, tenant=tenant)
    allocations = payment.allocations.select_related("customer_invoice", "supplier_invoice").all()
    return render(request, "payments/payment_detail.html", {
        "tenant": tenant, "payment": payment, "allocations": allocations,
    })


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def bank_reconciliation(request):
    tenant = _get_default_tenant(request)
    payments = Payment.objects.filter(tenant=tenant, status=Payment.Status.POSTED).select_related("customer", "supplier").order_by("payment_date", "id")

    if request.method == "POST":
        cleared_ids = set(request.POST.getlist("cleared"))
        for p in payments:
            should = str(p.id) in cleared_ids
            if should != p.is_reconciled:
                p.is_reconciled = should
                p.reconciled_at = timezone.now() if should else None
                p.save(update_fields=["is_reconciled", "reconciled_at"])
        messages.success(request, "Reconciliation saved.")
        return redirect("bank_reconciliation")

    def signed(p):
        return p.amount if p.direction == Payment.Direction.RECEIPT else -p.amount

    book_balance = sum((signed(p) for p in payments), Decimal("0.00"))
    cleared_balance = sum((signed(p) for p in payments if p.is_reconciled), Decimal("0.00"))
    return render(request, "payments/bank_reconciliation.html", {
        "tenant": tenant, "payments": payments,
        "book_balance": book_balance, "cleared_balance": cleared_balance,
        "uncleared": book_balance - cleared_balance,
    })


# ============================
# VAT return (MTD 9-box)
# ============================

@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def vat_index(request):
    tenant = _get_default_tenant(request)
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    preview = None
    if date_from and date_to:
        preview = vat_service.compute_vat_return(tenant, date_from, date_to)
    returns = VatReturn.objects.filter(tenant=tenant)
    return render(request, "vat/index.html", {
        "tenant": tenant, "returns": returns, "preview": preview,
        "date_from": date_from, "date_to": date_to,
    })


@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
@transaction.atomic
def vat_save(request):
    tenant = _get_default_tenant(request)
    if request.method != "POST":
        return redirect("vat_index")
    date_from = _parse_date(request.POST.get("from"))
    date_to = _parse_date(request.POST.get("to"))
    if not (date_from and date_to) or date_to < date_from:
        messages.error(request, "Please provide a valid period (from / to).")
        return redirect("vat_index")
    vr = vat_service.save_vat_return(tenant, date_from, date_to)
    messages.success(request, "VAT return saved as draft.")
    return redirect("vat_detail", vr_id=vr.id)


@role_required([ROLE_FINANCE, ROLE_ADMIN, ROLE_READONLY], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
def vat_detail(request, vr_id):
    tenant = _get_default_tenant(request)
    vr = get_object_or_404(VatReturn, id=vr_id, tenant=tenant)
    return render(request, "vat/detail.html", {"tenant": tenant, "vr": vr})


@role_required([ROLE_FINANCE, ROLE_ADMIN], write_groups=[ROLE_FINANCE, ROLE_ADMIN])
@transaction.atomic
def vat_submit(request, vr_id):
    tenant = _get_default_tenant(request)
    vr = get_object_or_404(VatReturn, id=vr_id, tenant=tenant)
    if request.method == "POST":
        vat_service.submit_vat_return(vr, user=request.user)
        messages.warning(
            request,
            "Return marked submitted locally. Live HMRC MTD filing is not yet "
            "connected (needs HMRC credentials) - reference is a local stub.",
        )
    return redirect("vat_detail", vr_id=vr.id)


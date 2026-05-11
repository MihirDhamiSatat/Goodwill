# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


import json

import frappe
from frappe.query_builder import DocType, Order
from frappe.utils import cint, get_datetime
from frappe.utils.nestedset import get_root_of

from erpnext.accounts.doctype.pos_invoice.pos_invoice import get_item_group, get_stock_availability
from erpnext.accounts.doctype.pos_profile.pos_profile import get_child_nodes, get_item_groups
from erpnext.stock.get_item_details import get_conversion_factor
from erpnext.stock.utils import scan_barcode


def search_by_term(search_term, warehouse, price_list):
	result = search_for_serial_or_batch_or_barcode_number(search_term) or {}

	item_code = result.get("item_code", search_term)
	serial_no = result.get("serial_no", "")
	batch_no = result.get("batch_no", "")
	barcode = result.get("barcode", "")

	if not result:
		return

	item_doc = frappe.get_doc("Item", item_code)

	if not item_doc:
		return

	item = {
		"barcode": barcode,
		"batch_no": batch_no,
		"description": item_doc.description,
		"is_stock_item": item_doc.is_stock_item,
		"item_code": item_doc.name,
		"item_group": item_doc.item_group,
		"item_image": item_doc.image,
		"item_name": item_doc.item_name,
		"serial_no": serial_no,
		"stock_uom": item_doc.stock_uom,
		"uom": item_doc.stock_uom,
	}

	if barcode:
		barcode_info = next(filter(lambda x: x.barcode == barcode, item_doc.get("barcodes", [])), None)
		if barcode_info and barcode_info.uom:
			uom = next(filter(lambda x: x.uom == barcode_info.uom, item_doc.uoms), {})
			item.update(
				{
					"uom": barcode_info.uom,
					"conversion_factor": uom.get("conversion_factor", 1),
				}
			)

	item_stock_qty, is_stock_item, is_negative_stock_allowed = get_stock_availability(item_code, warehouse)
	item_stock_qty = item_stock_qty // item.get("conversion_factor", 1)
	item.update({"actual_qty": item_stock_qty})

	price_filters = {
		"price_list": price_list,
		"item_code": item_code,
	}

	if batch_no:
		price_filters["batch_no"] = ["in", [batch_no, ""]]

	if serial_no:
		price_filters["uom"] = item_doc.stock_uom

	price = frappe.get_list(
		doctype="Item Price",
		filters=price_filters,
		fields=["uom", "currency", "price_list_rate", "batch_no"],
	)

	def __sort(p):
		p_uom = p.get("uom")
		p_batch = p.get("batch_no")
		batch_no = item.get("batch_no")

		if batch_no and p_batch and p_batch == batch_no:
			if p_uom == item.get("uom"):
				return 0
			elif p_uom == item.get("stock_uom"):
				return 1
			else:
				return 2

		if p_uom == item.get("uom"):
			return 3
		elif p_uom == item.get("stock_uom"):
			return 4
		else:
			return 5

	# sort by fallback preference. always pick exact uom and batch number match if available
	price = sorted(price, key=__sort)

	if len(price) > 0:
		p = price.pop(0)
		item.update(
			{
				"currency": p.get("currency"),
				"price_list_rate": p.get("price_list_rate"),
			}
		)

	return {"items": [item]}


def filter_result_items(result, pos_profile):
	if result and result.get("items"):
		pos_profile_doc = frappe.get_cached_doc("POS Profile", pos_profile)
		pos_item_groups = get_item_group(pos_profile_doc)
		if not pos_item_groups:
			return
		result["items"] = [item for item in result.get("items") if item.get("item_group") in pos_item_groups]


@frappe.whitelist()
def get_parent_item_group(pos_profile):
	item_groups = get_item_groups(pos_profile)

	if not item_groups:
		item_groups = frappe.get_all("Item Group", {"lft": 1, "is_group": 1}, pluck="name")

	return item_groups[0] if item_groups else None


@frappe.whitelist()
def get_items(start, page_length, price_list, item_group, pos_profile, search_term=""):
	warehouse, hide_unavailable_items = frappe.db.get_value(
		"POS Profile", pos_profile, ["warehouse", "hide_unavailable_items"]
	)

	result = []

	if search_term:
		result = search_by_term(search_term, warehouse, price_list) or []
		filter_result_items(result, pos_profile)
		if result:
			return result

	if not frappe.db.exists("Item Group", item_group):
		item_group = get_root_of("Item Group")

	condition = get_conditions(search_term)
	condition += get_item_group_condition(pos_profile)

	lft, rgt = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"])

	bin_join_selection, bin_join_condition = "", ""
	if hide_unavailable_items:
		bin_join_selection = "LEFT JOIN `tabBin` bin ON bin.item_code = item.name"
		bin_join_condition = "AND (item.is_stock_item = 0 OR (item.is_stock_item = 1 AND bin.warehouse = %(warehouse)s AND bin.actual_qty > 0))"

	items_data = frappe.db.sql(
		"""
		SELECT
			item.name AS item_code,
			item.item_name,
			item.description,
			item.stock_uom,
			item.image AS item_image,
			item.is_stock_item,
			item.sales_uom
		FROM
			`tabItem` item {bin_join_selection}
		WHERE
			item.disabled = 0
			AND item.has_variants = 0
			AND item.is_sales_item = 1
			AND item.is_fixed_asset = 0
			AND item.item_group in (SELECT name FROM `tabItem Group` WHERE lft >= {lft} AND rgt <= {rgt})
			AND {condition}
			{bin_join_condition}
		ORDER BY
			item.name asc
		LIMIT
			{page_length} offset {start}""".format(
			start=cint(start),
			page_length=cint(page_length),
			lft=cint(lft),
			rgt=cint(rgt),
			condition=condition,
			bin_join_selection=bin_join_selection,
			bin_join_condition=bin_join_condition,
		),
		{"warehouse": warehouse},
		as_dict=1,
	)

	# return (empty) list if there are no results
	if not items_data:
		return result

	current_date = frappe.utils.today()

	for item in items_data:
		item.actual_qty, _, is_negative_stock_allowed = get_stock_availability(item.item_code, warehouse)

		ItemPrice = DocType("Item Price")
		item_prices = (
			frappe.qb.from_(ItemPrice)
			.select(
				ItemPrice.price_list_rate,
				ItemPrice.currency,
				ItemPrice.uom,
				ItemPrice.batch_no,
				ItemPrice.valid_from,
				ItemPrice.valid_upto,
			)
			.where(ItemPrice.price_list == price_list)
			.where(ItemPrice.item_code == item.item_code)
			.where(ItemPrice.selling == 1)
			.where((ItemPrice.valid_from <= current_date) | (ItemPrice.valid_from.isnull()))
			.where((ItemPrice.valid_upto >= current_date) | (ItemPrice.valid_upto.isnull()))
			.orderby(ItemPrice.valid_from, order=Order.desc)
		).run(as_dict=True)

		stock_uom_price = next((d for d in item_prices if d.get("uom") == item.stock_uom), {})
		item_uom = item.stock_uom
		item_uom_price = stock_uom_price

		if item.sales_uom and item.sales_uom != item.stock_uom:
			item_uom = item.sales_uom
			sales_uom_price = next((d for d in item_prices if d.get("uom") == item.sales_uom), {})
			if sales_uom_price:
				item_uom_price = sales_uom_price

		if item_prices and not item_uom_price:
			item_uom = item_prices[0].get("uom")
			item_uom_price = item_prices[0]

		item_conversion_factor = get_conversion_factor(item.item_code, item_uom).get("conversion_factor")

		if item.stock_uom != item_uom:
			item.actual_qty = item.actual_qty // item_conversion_factor

		if item_uom_price and item_uom != item_uom_price.get("uom"):
			item_uom_price.price_list_rate = item_uom_price.price_list_rate * item_conversion_factor

		result.append(
			{
				**item,
				"price_list_rate": item_uom_price.get("price_list_rate"),
				"currency": item_uom_price.get("currency"),
				"uom": item_uom,
				"batch_no": item_uom_price.get("batch_no"),
			}
		)

	return {"items": result}


@frappe.whitelist()
def search_for_serial_or_batch_or_barcode_number(search_value: str) -> dict[str, str | None]:
	return scan_barcode(search_value)


def get_conditions(search_term):
	condition = "("
	condition += """item.name like {search_term}
		or item.item_name like {search_term}""".format(search_term=frappe.db.escape("%" + search_term + "%"))
	condition += add_search_fields_condition(search_term)
	condition += ")"

	return condition


def add_search_fields_condition(search_term):
	condition = ""
	search_fields = frappe.get_all("POS Search Fields", fields=["fieldname"])
	if search_fields:
		for field in search_fields:
			if not field.get("fieldname"):
				continue
			condition += " or item.`{}` like {}".format(
				field["fieldname"], frappe.db.escape("%" + search_term + "%")
			)
	return condition


def get_item_group_condition(pos_profile):
	cond = "and 1=1"
	item_groups = get_item_groups(pos_profile)
	if item_groups:
		cond = "and item.item_group in (%s)" % (", ".join(["%s"] * len(item_groups)))

	return cond % tuple(item_groups)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def item_group_query(doctype, txt, searchfield, start, page_len, filters):
	item_groups = []
	cond = "1=1"
	pos_profile = filters.get("pos_profile")

	if pos_profile:
		item_groups = get_item_groups(pos_profile)

		if item_groups:
			cond = "name in (%s)" % (", ".join(["%s"] * len(item_groups)))
			cond = cond % tuple(item_groups)

	return frappe.db.sql(
		f""" select distinct name from `tabItem Group`
			where {cond} and (name like %(txt)s) limit {page_len} offset {start}""",
		{"txt": "%%%s%%" % txt},
	)


@frappe.whitelist()
def check_opening_entry(user):
	open_vouchers = frappe.db.get_all(
		"POS Opening Entry",
		filters={"user": user, "pos_closing_entry": ["in", ["", None]], "docstatus": 1},
		fields=["name", "company", "pos_profile", "period_start_date"],
		order_by="period_start_date desc",
	)

	return open_vouchers


@frappe.whitelist()
def create_opening_voucher(pos_profile, company, balance_details):
	balance_details = json.loads(balance_details)

	new_pos_opening = frappe.get_doc(
		{
			"doctype": "POS Opening Entry",
			"period_start_date": frappe.utils.get_datetime(),
			"posting_date": frappe.utils.getdate(),
			"user": frappe.session.user,
			"pos_profile": pos_profile,
			"company": company,
		}
	)
	new_pos_opening.set("balance_details", balance_details)
	new_pos_opening.submit()

	return new_pos_opening.as_dict()


@frappe.whitelist()
def get_past_order_list(search_term, status, limit=20):
	fields = ["name", "grand_total", "currency", "customer", "customer_name", "posting_time", "posting_date"]
	invoice_list = []

	if search_term and status:
		pos_invoices_by_customer = frappe.db.get_list(
			"POS Invoice",
			filters=get_invoice_filters("POS Invoice", status),
			or_filters={
				"customer_name": ["like", f"%{search_term}%"],
				"customer": ["like", f"%{search_term}%"],
			},
			fields=fields,
			page_length=limit,
		)

		pos_invoices_by_name = frappe.db.get_list(
			"POS Invoice",
			filters=get_invoice_filters("POS Invoice", status, name=search_term),
			fields=fields,
			page_length=limit,
		)

		pos_invoice_list = add_doctype_to_results(
			"POS Invoice", pos_invoices_by_customer + pos_invoices_by_name
		)

		sales_invoices_by_customer = frappe.db.get_list(
			"Sales Invoice",
			filters=get_invoice_filters("Sales Invoice", status),
			or_filters={
				"customer_name": ["like", f"%{search_term}%"],
				"customer": ["like", f"%{search_term}%"],
			},
			fields=fields,
			page_length=limit,
		)
		sales_invoices_by_name = frappe.db.get_list(
			"Sales Invoice",
			filters=get_invoice_filters("Sales Invoice", status, name=search_term),
			fields=fields,
			page_length=limit,
		)

		sales_invoice_list = add_doctype_to_results(
			"Sales Invoice", sales_invoices_by_customer + sales_invoices_by_name
		)

	elif status:
		pos_invoice_list = frappe.db.get_list(
			"POS Invoice",
			filters=get_invoice_filters("POS Invoice", status),
			fields=fields,
			page_length=limit,
		)
		pos_invoice_list = add_doctype_to_results("POS Invoice", pos_invoice_list)

		sales_invoice_list = frappe.db.get_list(
			"Sales Invoice",
			filters=get_invoice_filters("Sales Invoice", status),
			fields=fields,
			page_length=limit,
		)
		sales_invoice_list = add_doctype_to_results("Sales Invoice", sales_invoice_list)

	invoice_list = order_results_by_posting_date([*pos_invoice_list, *sales_invoice_list])

	return invoice_list


@frappe.whitelist()
def set_customer_info(fieldname, customer, value=""):
	if fieldname == "loyalty_program":
		frappe.db.set_value("Customer", customer, "loyalty_program", value)

	contact = frappe.get_cached_value("Customer", customer, "customer_primary_contact")
	if not contact:
		contact = frappe.db.sql(
			"""
			SELECT parent FROM `tabDynamic Link`
			WHERE
				parenttype = 'Contact' AND
				parentfield = 'links' AND
				link_doctype = 'Customer' AND
				link_name = %s
			""",
			(customer),
			as_dict=1,
		)
		contact = contact[0].get("parent") if contact else None

	if not contact:
		new_contact = frappe.new_doc("Contact")
		new_contact.is_primary_contact = 1
		new_contact.first_name = customer
		new_contact.set("links", [{"link_doctype": "Customer", "link_name": customer}])
		new_contact.save()
		contact = new_contact.name
		frappe.db.set_value("Customer", customer, "customer_primary_contact", contact)

	contact_doc = frappe.get_doc("Contact", contact)
	if fieldname == "email_id":
		contact_doc.set("email_ids", [{"email_id": value, "is_primary": 1}])
		frappe.db.set_value("Customer", customer, "email_id", value)
	elif fieldname == "mobile_no":
		contact_doc.set("phone_nos", [{"phone": value, "is_primary_mobile_no": 1}])
		frappe.db.set_value("Customer", customer, "mobile_no", value)
	contact_doc.save()


@frappe.whitelist()
def get_pos_profile_data(pos_profile):
	pos_profile = frappe.get_doc("POS Profile", pos_profile)
	pos_profile = pos_profile.as_dict()

	_customer_groups_with_children = []
	for row in pos_profile.customer_groups:
		children = get_child_nodes("Customer Group", row.customer_group)
		_customer_groups_with_children.extend(children)

	pos_profile.customer_groups = _customer_groups_with_children
	return pos_profile


def add_doctype_to_results(doctype, results):
	for result in results:
		result["doctype"] = doctype

	return results


def order_results_by_posting_date(results):
	return sorted(
		results,
		key=lambda x: get_datetime(f"{x.get('posting_date')} {x.get('posting_time')}"),
		reverse=True,
	)


def get_invoice_filters(doctype, status, name=None):
	filters = {}

	if name:
		filters["name"] = ["like", f"%{name}%"]
	if doctype == "POS Invoice":
		filters["status"] = status
		if status == "Partly Paid":
			filters["status"] = ["in", ["Partly Paid", "Overdue", "Unpaid"]]
		return filters

	if doctype == "Sales Invoice":
		filters["is_created_using_pos"] = 1
		filters["is_consolidated"] = 0

		if status == "Consolidated":
			filters["pos_closing_entry"] = ["is", "set"]
		else:
			filters["pos_closing_entry"] = ["is", "not set"]
			if status == "Draft":
				filters["docstatus"] = 0
			elif status == "Partly Paid":
				filters["status"] = ["in", ["Partly Paid", "Overdue", "Unpaid"]]
			else:
				filters["docstatus"] = 1
				if status == "Paid":
					filters["is_return"] = 0
				if status == "Return":
					filters["is_return"] = 1

	return filters


@frappe.whitelist()
def get_customer_recent_transactions(customer):
	sales_invoices = frappe.db.get_list(
		"Sales Invoice",
		filters={
			"customer": customer,
			"docstatus": 1,
			"is_pos": 1,
			"is_consolidated": 0,
			"is_created_using_pos": 1,
		},
		fields=["name", "grand_total", "status", "posting_date", "posting_time", "currency"],
		page_length=20,
	)

	pos_invoices = frappe.db.get_list(
		"POS Invoice",
		filters={"customer": customer, "docstatus": 1},
		fields=["name", "grand_total", "status", "posting_date", "posting_time", "currency"],
		page_length=20,
	)

	invoices = order_results_by_posting_date(sales_invoices + pos_invoices)
	return invoices


@frappe.whitelist()
def validate_pos_invoice_for_goodwill(doctype, docname):
	"""
	PREVENT POS INVOICE SUBMISSION FOR GOODWILL CUSTOMERS
	Goodwill customers should only create Stock Entry, not Invoice
	"""
	if doctype not in ["POS Invoice", "Sales Invoice"]:
		return True
	
	# Get the invoice document
	invoice = frappe.get_doc(doctype, docname)
	
	# Check if customer is marked as goodwill customer
	customer = invoice.customer
	if customer:
		is_goodwill_customer = frappe.db.get_value("Customer", customer, "is_goodwill_customer")
		
		if is_goodwill_customer:
			# For goodwill customers, check if a goodwill stock entry exists
			# If not, prevent invoice submission
			has_goodwill_entry = frappe.db.exists("Stock Entry", {
				"goodwill": ["!=", ""],
				"docstatus": 1,
				"creation": [">", frappe.utils.add_days(frappe.utils.now(), -1)]
			})
			
			# Prevent POS Invoice submission for goodwill customers
			frappe.throw(
				f"<b>Goodwill Customer Cannot Submit Invoice</b><br>"
				f"Customer '{customer}' is marked as a goodwill customer.<br>"
				f"Please use the <b>Goodwill Entry</b> feature instead to create a Stock Entry.<br>"
				f"<i>Note: POS invoices cannot be created for goodwill customers - only Stock Entries.</i>",
				frappe.ValidationError
			)


@frappe.whitelist()
def create_goodwill_stock_entry(goodwill_entry_id, items, customer, company, warehouse):
	"""
	Create Stock Entry (Material Issue) for goodwill distribution using VALUATION RATE
	"""
	import json
	from frappe.utils import nowdate, nowtime
	from erpnext.stock.stock_ledger import get_previous_sle
	
	# Parse items if it's a JSON string
	if isinstance(items, str):
		items = json.loads(items)
	
	# Validate inputs
	if not goodwill_entry_id or not items:
		frappe.throw("Goodwill Entry and items are required")
	
	# Get goodwill entry
	goodwill = frappe.get_doc("Goodwill", goodwill_entry_id)
	if goodwill.is_used:
		frappe.throw(f"Goodwill Entry {goodwill_entry_id} has already been used")
	
	# Create Stock Entry
	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.stock_entry_type = "Goodwill Issue"
	stock_entry.purpose = "Goodwill Issue"
	stock_entry.company = company
	stock_entry.posting_date = nowdate()
	stock_entry.posting_time = nowtime()
	stock_entry.remarks = f"Goodwill Distribution - Customer: {customer}, Goodwill: {goodwill_entry_id}"
	stock_entry.goodwill = goodwill_entry_id
	
	total_amount = 0
	
	# Add items with valuation rate from cost price
	for item in items:
		item_code = item.get("item_code")
		qty = float(item.get("qty", 0))
		uom = item.get("uom")
		
		# Get valuation rate from stock ledger
		prev_sle = get_previous_sle({
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
		})
		
		rate = prev_sle.get("valuation_rate", 0) if prev_sle else 0
		amount = qty * rate
		total_amount += amount
		
		stock_entry.append("items", {
			"item_code": item_code,
			"qty": qty,
			"uom": uom,
			"s_warehouse": warehouse,
			"basic_rate": rate,
			"amount": amount,
		})
	
	# Save and submit
	stock_entry.insert(ignore_permissions=True)
	stock_entry.submit()
	
	# Mark goodwill as used (use db.set_value to bypass submission lock on submitted doc)
	frappe.db.set_value("Goodwill", goodwill_entry_id, "is_used", 1)
	
	frappe.msgprint(
		f"Stock Entry <b>{stock_entry.name}</b> created for Goodwill Distribution<br>"
		f"Total Cost: ₹{total_amount:.2f}<br>"
		f"Stock reduced from: {warehouse}"
	)
	
	return stock_entry.name

@frappe.whitelist()
def create_subcenter(subcenter_name, user, parent_warehouse, company):
	"""
	Create a new sub-center with warehouse and POS profile.
	Inherits ALL account settings, taxes, payment methods, and configurations from parent POS Profile.
	Stores reference to parent profile for traceability.
	"""
	try:
		# Validate parent warehouse exists
		if not frappe.db.exists("Warehouse", parent_warehouse):
			frappe.throw(f"Parent Warehouse '{parent_warehouse}' does not exist")
		
		# Check if subcenter warehouse already exists
		subcenter_warehouse = frappe.db.get_value(
			"Warehouse",
			{"parent_warehouse": parent_warehouse, "warehouse_name": subcenter_name},
			"name"
		)
		if subcenter_warehouse:
			frappe.throw(f"Sub Center '{subcenter_name}' already exists under this warehouse")
		
		# Get parent POS profile by parent warehouse
		parent_pos_name = frappe.db.get_value(
			"POS Profile",
			{"warehouse": parent_warehouse},
			"name"
		)
		
		if not parent_pos_name:
			frappe.throw(f"No POS Profile found for parent warehouse '{parent_warehouse}'")
		
		parent_pos_doc = frappe.get_doc("POS Profile", parent_pos_name)
		
		# 1. Create New Warehouse
		warehouse_doc = frappe.get_doc({
			"doctype": "Warehouse",
			"warehouse_name": subcenter_name,
			"parent_warehouse": parent_warehouse,
			"company": company,
			"disabled": 0
		})
		warehouse_doc.insert()
		warehouse_name = warehouse_doc.name
		
		# 2. Get cost center from parent POS Profile (NOT warehouse)
		cost_center = parent_pos_doc.cost_center
		
		# 3. Create New POS Profile (Sub-Center) with ALL fields from parent
		pos_profile_name = f"POS-{subcenter_name}"
		
		# Get owner email from parent POS profile if available
		parent_owner_email = frappe.db.get_value("User", parent_pos_doc.owner or parent_pos_doc.user, "email")
		
		pos_profile_doc = frappe.get_doc({
			"doctype": "POS Profile",
			"name": pos_profile_name,
			"company": company,
			"warehouse": warehouse_name,
			"cost_center": cost_center,
			"is_subcenter": 1,
			"parent_profile": parent_pos_name,  # Store parent POS profile reference
			# Copy Core Settings
			"customer": parent_pos_doc.customer,
			"country": parent_pos_doc.country,
			# Copy Account Settings
			"income_account": parent_pos_doc.income_account,
			"expense_account": parent_pos_doc.expense_account,
			"write_off_account": parent_pos_doc.write_off_account,
			"write_off_cost_center": parent_pos_doc.write_off_cost_center,
			"write_off_limit": parent_pos_doc.write_off_limit,
			"account_for_change_amount": parent_pos_doc.account_for_change_amount,
			# Copy Tax Settings
			"taxes_and_charges": parent_pos_doc.taxes_and_charges,
			"tax_category": parent_pos_doc.tax_category,
			# Copy Price & Currency Settings
			"selling_price_list": parent_pos_doc.selling_price_list,
			"currency": parent_pos_doc.currency,
		})
		
		# Copy Item Groups
		if parent_pos_doc.item_groups:
			for item_group in parent_pos_doc.item_groups:
				pos_profile_doc.append("item_groups", {
					"item_group": item_group.item_group
				})
		
		# Copy Customer Groups
		if parent_pos_doc.customer_groups:
			for customer_group in parent_pos_doc.customer_groups:
				pos_profile_doc.append("customer_groups", {
					"customer_group": customer_group.customer_group
				})
		
		# Copy Payment Methods (CRITICAL) - All MOPs from parent
		if parent_pos_doc.payments:
			for payment in parent_pos_doc.payments:
				pos_profile_doc.append("payments", {
					"mode_of_payment": payment.mode_of_payment,
					"default": payment.default,
					"allow_in_returns": payment.allow_in_returns
				})
		
		if user:
			pos_profile_doc.append("applicable_for_users", {"user": user, "default": 1})

		pos_profile_doc.insert()
		frappe.db.commit()
		
		return {
			"warehouse": warehouse_name,
			"pos_profile": pos_profile_name,
			"cost_center": cost_center,
			"parent_pos_profile": parent_pos_name,
			"user": user,
			"company": company
		}
		
	except Exception as e:
		frappe.log_error(f"Error creating sub-center: {str(e)}")
		frappe.throw(f"Failed to create sub-center: {str(e)}")
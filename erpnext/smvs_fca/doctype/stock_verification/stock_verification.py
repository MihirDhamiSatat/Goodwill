# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class StockVerification(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from erpnext.smvs_fca.doctype.stock_verification_item.stock_verification_item import StockVerificationItem
		from frappe.types import DF

		cause_description: DF.SmallText | None
		naming_series: DF.Literal["SD-.YYYY.-.#####"]
		root_cause_type: DF.Literal["", "HO Mistake", "Store Mistake", "Transit Loss"]
		source_warehouse: DF.Data | None
		status: DF.Literal["Pending", "Verified", "Issue Found", "Resolved"]
		stock_entry: DF.Link
		stock_verification_item: DF.Table[StockVerificationItem]
		target_warehouse: DF.Data | None
	# end: auto-generated types

	pass

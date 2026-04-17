# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class Goodwill(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		allocated_to: DF.Link
		amended_from: DF.Link | None
		approved_by: DF.Link
		company: DF.Link | None
		is_used: DF.Check
		naming_series: DF.Literal[None]
		posting_date: DF.Date | None
		reason: DF.SmallText
	# end: auto-generated types

	pass

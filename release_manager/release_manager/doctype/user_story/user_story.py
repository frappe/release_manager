# Copyright (c) 2026, Release Manager Contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

REVIEWED = ("Approved", "Needs Change", "Rejected")


class UserStory(Document):
	def validate(self):
		if self.status in ("Needs Change", "Rejected") and not (self.review_notes or "").strip():
			frappe.throw(_("Add review notes saying what should change, or why the story is rejected."))
		if self.status == "Approved" and not (self.expected_results or "").strip():
			frappe.throw(_("A story needs at least one expected result before it can be approved."))
		if self.has_value_changed("status") and self.status in REVIEWED:
			self.reviewed_by = frappe.session.user
			self.reviewed_on = now_datetime()
		if self.has_value_changed("expected_results") and not self.is_new():
			# the generated checks no longer match what the story asks for
			self.check_list_confirmed = 0

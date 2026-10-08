"""Control-plane persistence: the platform's own database.

Strictly separate from the customer's database, which stays read-only. This
package owns the fire ledger, stored credentials, definition versions, and
delivery records.
"""

# Access Rights Management Module

## Overview
This module provides custom user groups with specific access rights for subscription management and invoicing functionalities.

## User Groups

### 1. Support Staff
**Description:** Entry-level support personnel with read-only access.

**Permissions:**
- **Read-only** access to:
  - Subscription Management (all models)
  - Invoicing (account.move, account.payment, etc.)
  - Partners/Customers
  - Products

**Use Case:** Support staff who need to view subscription and invoice information to assist customers but should not modify data.

---

### 2. Support Staff Manager
**Description:** Senior support personnel with read/write access.

**Permissions:**
- **Read/Write** access to:
  - Subscription Management (all models)
  - Invoicing (account.move, account.payment, etc.)
  - Partners/Customers
  - Products
- **Cannot delete** critical records like invoices and payments (for audit purposes)

**Use Case:** Support managers who need to update subscriptions, create invoices, and manage customer accounts.

---

### 3. Admin
**Description:** Administrative users with full access except system settings.

**Permissions:**
- **Full access** (read/write/create/delete) to:
  - Subscription Management
  - Invoicing
  - Sales
  - Customers/Partners
  - Products
- **Cannot access:** System Settings and Technical Settings

**Use Case:** Business administrators who manage day-to-day operations but don't need system configuration access.

---

### 4. Technical Manager
**Description:** Technical administrators with complete system access.

**Permissions:**
- **Full access** to everything, including:
  - All Admin permissions
  - System Settings
  - Technical Settings
  - Developer Mode features
  - Module installation/configuration

**Use Case:** Technical leads and developers who need to configure the system, install modules, and manage technical settings.

---

## Odoo Administrator
The built-in Odoo Administrator account automatically has all access rights and is not restricted by this module.

---

## Installation

1. Copy this module to your Odoo addons directory
2. Update the apps list: Settings → Apps → Update Apps List
3. Search for "Access Rights Management"
4. Click Install

## Configuration

After installation, assign users to appropriate groups:

1. Go to: Settings → Users & Companies → Users
2. Select a user
3. Go to the "Access Rights" tab
4. Select the appropriate group from the "Access Rights" category

## Dependencies

- `base` - Odoo core
- `subscription_management` - Custom subscription management module
- `account` - Odoo Accounting/Invoicing module

## Technical Details

### Models with Access Control

**Subscription Management:**
- subscription.subscription
- subscription.plan
- subscription.discount
- subscription.reasons
- zeeve.invoice
- zeeve.invoice.line
- stripe.payment.log

**Accounting/Invoicing:**
- account.move (Invoices)
- account.move.line (Invoice Lines)
- account.payment (Payments)
- account.journal (Journals)

**Base:**
- res.partner (Customers/Partners)
- product.product
- product.template

### Security Files

- `security/access_rights_groups.xml` - Defines user groups and their hierarchy
- `security/ir.model.access.csv` - Model-level access rights for each group

## Notes

- The group hierarchy is: Support Staff → Support Staff Manager → Admin → Technical Manager
- Each higher-level group inherits permissions from lower-level groups
- The Admin group specifically excludes access to the Settings menu
- Support Staff and Support Staff Manager cannot delete invoices or payments (audit trail protection)

## Support

For issues or questions, contact the development team.

## License

LGPL-3


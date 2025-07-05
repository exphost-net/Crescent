## Crescent Alpha Release v0.2

Welcome to Crescent Alpha v.0.2!

### What's Changed?

#### Features
* **Dynamic Currency Symbols**: Currency symbols now dynamically update based on the selected base currency across all pages, including `Outgoings` and `Incomings`.
* **Support for Multiple Currencies**: Added support for multiple currencies with automatic conversion based on exchange rates.
* **Coupon Integration**: Added coupon checking and display in the `Incomings` page.
* **Customer Billing Dates**: Added customer billing dates in the `Incomings` page.
* **Machine Billing Dates**: Added machine billing dates in the `Outgoings` page.

#### Improvements
* **Graceful Handling of Missing Data**: Crescent no longer crashes if no program (e.g., Paymenter or Pterodactyl) is connected. It now gracefully shows "No data available."
* **UI Enhancements**: Updated the design of the `Outgoings`, `Incomings`, and `Settings` pages for better usability and responsiveness.
* **Error Handling**: Improved error handling and user feedback for database and API-related issues.
* **Code Refactoring**: Simplified and optimized backend logic for better maintainability.

#### Bug Fixes
* Fixed hardcoded `$` symbols in the `Outgoings` page to use dynamic currency symbols.
* Resolved issues with orphaned machine costs in the `Outgoings` page.
* Addressed inconsistencies in the `Incomings` page for displaying original prices and discounts.

### Security Vulnerabilities

If you discover any security bugs or issues, please help us improve Crescent by opening a ticket directly in the ExpHost Discord. Your responsible disclosure is greatly appreciated.

### Notes

* This is an alpha release, so expect bugs and limited features. Your feedback is invaluable in helping us improve Crescent.
* For general support, visit our Git issues: [https://github.com/exphost-net/Crescent/issues](https://github.com/exphost-net/Crescent/issues) or join our community on the ExpHost Discord.

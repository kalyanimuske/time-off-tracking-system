"""Simulated versions of the services the prompt says already exist.

    Employee Service  employees, pay rates, work configuration
    Company Service   company information
    Payroll Service   hours worked and pay per period, publishes on_payroll_processed

In a real system these are separate services owned by other teams. They're
small here, and kept in their own package, so the Time Off Service can be
demoed end to end. The Time Off Service only talks to them through the methods
and the event defined here, the same way it would talk to the real ones.
"""

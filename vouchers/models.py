# vouchers/models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import os


class Designation(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='designations')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    designation = models.ForeignKey(Designation, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.user.username} - {self.designation}"

class Voucher(models.Model):
    PAYMENT_TYPES = (
        ('CASH', 'Cash'),
        ('CHEQUE', 'Cheque'),
        ('PETTY_CASH', 'Petty Cash'),
    )

    voucher_number = models.CharField(max_length=20, unique=True, blank=True)
    voucher_date = models.DateField()
    payment_type = models.CharField(max_length=20, choices=PAYMENT_TYPES)
    name_title = models.CharField(max_length=5, choices=[('MR', 'Mr.'), ('MRS', 'Mrs.'), ('MS', 'Ms.')])
    pay_to = models.CharField(max_length=200)
    attachment = models.FileField(upload_to='vouchers/attachments/', null=True, blank=True)

    # NEW: CHEQUE NUMBER
    cheque_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Required only for Cheque payments"
    )

    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='vouchers')
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=10,
        choices=[('PENDING', 'Pending'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected')],
        default='PENDING'
    )

    def save(self, *args, **kwargs):
        if not self.voucher_number:
            last_voucher = Voucher.objects.order_by('-id').first()
            if last_voucher and last_voucher.voucher_number.startswith('VCH'):
                num = int(last_voucher.voucher_number[3:]) + 1
                self.voucher_number = f'VCH{num:04d}'
            else:
                self.voucher_number = 'VCH0001'
        super().save(*args, **kwargs)

    def __str__(self):
        return self.voucher_number

    @property
    def required_approvers(self):
        active_des_ids = ActiveApprovalDesignation.objects.filter(
            is_active=True
        ).values_list('designation__id', flat=True)
        
        return [
            profile.user.username for profile in UserProfile.objects.filter(
                user__groups__name='Admin Staff',
                designation__id__in=active_des_ids
            ).select_related('user').distinct()
        ]

    def _update_status_if_all_approved(self):
        required = self.required_approvers
        if not required:
            return

        approved_count = self.approvals.filter(status='APPROVED').count()
        has_rejection = self.approvals.filter(status='REJECTED').exists()

        if has_rejection:
            self.status = 'REJECTED'
        elif approved_count == len(required):
            self.status = 'APPROVED'
        else:
            self.status = 'PENDING'

        self.save(update_fields=['status'])


class Particular(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name='particulars')
    description = models.CharField(max_length=300)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    attachment = models.FileField(
        upload_to='vouchers/particulars/',
        null=True,
        blank=True,
        help_text="Attach receipt/invoice for this item"
    )

    def __str__(self):
        return f"{self.description} - {self.amount}"


class VoucherApproval(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name='approvals')
    approver = models.ForeignKey(User, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=[('APPROVED', 'Approved'), ('REJECTED', 'Rejected')])
    approved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('voucher', 'approver')

    def __str__(self):
        return f"{self.approver} - {self.status}"


class ActiveApprovalDesignation(models.Model):
    designation = models.ForeignKey(Designation, on_delete=models.CASCADE, unique=True)
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(User, on_delete=models.CASCADE)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Active Approval Designation"
        verbose_name_plural = "Active Approval Designations"

    def __str__(self):
        return f"{self.designation.name} - {'Active' if self.is_active else 'Inactive'}"
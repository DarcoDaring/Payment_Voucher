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

    # NEW: CHEQUE ATTACHMENT
    cheque_attachment = models.FileField(
        upload_to='vouchers/cheques/',
        null=True,
        blank=True,
        help_text="Required only for Cheque payments"
    )

    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='vouchers')
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=10,
        choices=[('PENDING', 'Pending'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected')],
        default='PENDING'
    )

    # NEW: Store required approvers at creation time
    required_approvers_snapshot = models.JSONField(
        default=list,
        blank=True,
        help_text="List of usernames required at voucher creation time"
    )

    def save(self, *args, **kwargs):
        if not self.voucher_number:
            last_voucher = Voucher.objects.order_by('-id').first()
            if last_voucher and last_voucher.voucher_number.startswith('VCH'):
                num = int(last_voucher.voucher_number[3:]) + 1
                self.voucher_number = f'VCH{num:04d}'
            else:
                self.voucher_number = 'VCH0001'

        # Save snapshot on first save (creation)
        if not self.pk:
            super().save(*args, **kwargs)  # Save first to get PK
            self.required_approvers_snapshot = self._get_current_required_approvers()
            self.save(update_fields=['required_approvers_snapshot'])
        else:
            super().save(*args, **kwargs)

    def _get_current_required_approvers(self):
        """Helper: Get current required approvers from active levels."""
        levels = ApprovalLevel.objects.filter(is_active=True).select_related('designation').order_by('order')
        usernames = []
        for level in levels:
            users = UserProfile.objects.filter(
                designation=level.designation,
                user__groups__name='Admin Staff'
            ).values_list('user__username', flat=True).distinct()
            usernames.extend(users)
        return usernames

    def __str__(self):
        return self.voucher_number

    @property
    def required_approvers(self):
        """
        Return required approvers:
        - For APPROVED/REJECTED: Use snapshot (locked at creation)
        - For PENDING: Use current active levels (dynamic)
        """
        if self.status in ['APPROVED', 'REJECTED']:
            return self.required_approvers_snapshot or []
        else:
            return self._get_current_required_approvers()

    def _update_status_if_all_approved(self):
        """Update voucher status based on approvals and rejections."""
        required = self.required_approvers  # Uses snapshot for approved, dynamic for pending

        if not required:
            self.status = 'APPROVED'
            self.save(update_fields=['status'])
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


# === ORDERED APPROVAL LEVELS ===
class ApprovalLevel(models.Model):
    designation = models.OneToOneField(Designation, on_delete=models.CASCADE)
    order = models.PositiveIntegerField(unique=True, help_text="Lower number = earlier in approval chain")
    is_active = models.BooleanField(default=True, help_text="Only active levels require approval")
    updated_by = models.ForeignKey(User, on_delete=models.CASCADE)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order']
        verbose_name = "Approval Level"
        verbose_name_plural = "Approval Levels"

    def __str__(self):
        return f"{self.order}. {self.designation.name} ({'Active' if self.is_active else 'Inactive'})"
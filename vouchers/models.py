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
    
    # NEW: User signature
    signature = models.ImageField(
        upload_to='signatures/',
        null=True,
        blank=True,
        help_text="Upload your digital signature (PNG/JPG, transparent background recommended)"
    )

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
    
    # REMOVED: Old single main attachment (now using MainAttachment model below)
    # attachment = models.FileField(upload_to='vouchers/attachments/', null=True, blank=True)

    # CHEQUE FIELDS
    cheque_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Required only for Cheque payments"
    )

    # REMOVED: Old single cheque attachment (now using ChequeAttachment model below)
    # cheque_attachment = models.FileField(upload_to='vouchers/cheques/', null=True, blank=True)

    cheque_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date on the cheque - required for Cheque payments"
    )

    # ACCOUNT DETAILS → REQUIRED FOR CHEQUE
    account_details = models.ForeignKey(
        'AccountDetail',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Bank account for Cheque payments"
     )

    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='vouchers')
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=10,
        choices=[('PENDING', 'Pending'), ('APPROVED', 'Approved'), ('REJECTED', 'Rejected')],
        default='PENDING'
    )

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
        if self.status in ['APPROVED', 'REJECTED']:
            return self.required_approvers_snapshot or []
        else:
            return self._get_current_required_approvers()

    def _update_status_if_all_approved(self):
        required = self.required_approvers
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

    def clean(self):
        from django.core.exceptions import ValidationError

        # CHEQUE VALIDATION
        if self.payment_type == 'CHEQUE':
            if not self.cheque_number:
                raise ValidationError("Cheque number is required for Cheque payments.")
            if not self.cheque_attachments.exists():  # Now checks related objects
                raise ValidationError("At least one cheque attachment is required for Cheque payments.")
            if not self.cheque_date:
                raise ValidationError("Cheque date is required for Cheque payments.")
            if not self.account_details:
                raise ValidationError("Account Details is required for Cheque payments.")
        else:
            # Clear cheque fields if not CHEQUE
            self.cheque_number = None
            self.cheque_date = None
            self.account_details = None


class Particular(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name='particulars')
    description = models.CharField(max_length=300)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    
    # REMOVED: Old single attachment (now using ParticularAttachment model below)
    # attachment = models.FileField(upload_to='vouchers/particulars/', help_text="...")

    def __str__(self):
        return f"{self.description} - {self.amount}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if not self.attachments.exists():  # Now checks multiple attachments
            raise ValidationError("At least one attachment is required for each particular.")


class VoucherApproval(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name='approvals')
    approver = models.ForeignKey(User, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=[('APPROVED', 'Approved'), ('REJECTED', 'Rejected')])
    approved_at = models.DateTimeField(auto_now_add=True)
    rejection_reason = models.TextField(blank=True, null=True, help_text="Required when status is REJECTED")

    class Meta:
        unique_together = ('voucher', 'approver')

    def __str__(self):
        return f"{self.approver} - {self.status}"


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


class AccountDetail(models.Model):
    bank_name = models.CharField(max_length=200)
    account_number = models.CharField(max_length=50)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='account_details')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('bank_name', 'account_number')
        ordering = ['bank_name']

    def __str__(self):
        return f"{self.bank_name} / {self.account_number}"


# === NEW: COMPANY DETAIL (SINGLETON - ONLY ONE RECORD) ===
class CompanyDetail(models.Model):
    """
    matedocstring
    Singleton model: Only ONE company detail exists at a time.
    Used for: Company Name, GST, PAN, Address, Logo, Email, Phone.
    """
    name = models.CharField(max_length=200, help_text="Company Name")
    gst_no = models.CharField(max_length=20, blank=True, null=True, help_text="GST Number (e.g., 22AAAAA0000A1Z5)")
    pan_no = models.CharField(max_length=15, blank=True, null=True, help_text="PAN Number (e.g., AAAAA0000A)")
    address = models.TextField(blank=True, null=True, help_text="Full company address")
    email = models.EmailField(blank=True, null=True, help_text="Company email address")
    phone = models.CharField(max_length=20, blank=True, null=True, help_text="Company phone number")
    logo = models.ImageField(
        upload_to='company/logo/',
        null=True,
        blank=True,
        help_text="Company logo (PNG/JPG, max 2MB)"
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='company_details_updated'
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Company Detail"
        verbose_name_plural = "Company Details"

    def __str__(self):
        return self.name or "Company"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        CompanyDetail.objects.exclude(pk=1).delete()

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj


# =============================================
# NEW MODELS FOR MULTIPLE FILE UPLOADS
# =============================================

class MainAttachment(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name='main_attachments')
    file = models.FileField(upload_to='vouchers/main/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return os.path.basename(self.file.name)


class ChequeAttachment(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name='cheque_attachments')
    file = models.FileField(upload_to='vouchers/cheques/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return os.path.basename(self.file.name)


class ParticularAttachment(models.Model):
    particular = models.ForeignKey(Particular, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='vouchers/particulars/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return os.path.basename(self.file.name)
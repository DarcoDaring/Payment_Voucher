from rest_framework import serializers
from .models import Voucher, Particular, VoucherApproval, ApprovalLevel, AccountDetail, CompanyDetail  # ← Added CompanyDetail
from django.contrib.auth.models import User
from django.core.validators import FileExtensionValidator
from decimal import Decimal, InvalidOperation


class ParticularSerializer(serializers.ModelSerializer):
    attachment = serializers.FileField(
        required=True,  # ← REQUIRED NOW
        allow_null=False,
        validators=[
            FileExtensionValidator(allowed_extensions=['pdf', 'jpg', 'jpeg', 'png', 'doc', 'docx'])
        ]
    )

    class Meta:
        model = Particular
        fields = ['description', 'amount', 'attachment']

    def validate_amount(self, value):
        try:
            value_str = str(value).strip()
            value = Decimal(value_str)
        except (InvalidOperation, ValueError, TypeError):
            raise serializers.ValidationError("Invalid number format.")
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than 0.")
        return value

    def validate_attachment(self, value):
        if not value:
            raise serializers.ValidationError("Attachment is required for each particular.")
        if value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("File size cannot exceed 5 MB.")
        return value


class VoucherApprovalSerializer(serializers.ModelSerializer):
    approver = serializers.ReadOnlyField(source='approver.username')
    approved_at = serializers.DateTimeField(format="%d %b %H:%M", read_only=True)
    rejection_reason = serializers.CharField(read_only=True, allow_null=True, allow_blank=True)

    class Meta:
        model = VoucherApproval
        fields = ['approver', 'status', 'approved_at', 'rejection_reason']


# === NEW: AccountDetail Serializer for Dropdown ===
class AccountDetailSerializer(serializers.ModelSerializer):
    value = serializers.IntegerField(source='id')
    label = serializers.CharField(source='__str__')

    class Meta:
        model = AccountDetail
        fields = ['value', 'label']


class VoucherSerializer(serializers.ModelSerializer):
    created_by = serializers.ReadOnlyField(source='created_by.username')
    particulars = ParticularSerializer(many=True, required=True)
    
    # MAIN ATTACHMENT → OPTIONAL
    attachment = serializers.FileField(
        required=False,
        allow_null=True,
        validators=[
            FileExtensionValidator(
                allowed_extensions=['pdf', 'jpg', 'jpeg', 'png', 'doc', 'docx']
            )
        ]
    )

    # CHEQUE NUMBER
    cheque_number = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        allow_null=True
    )

    # NEW: CHEQUE ATTACHMENT
    cheque_attachment = serializers.FileField(
        required=False,
        allow_null=True,
        validators=[
            FileExtensionValidator(allowed_extensions=['pdf', 'jpg', 'jpeg', 'png'])
        ]
    )

    # ACCOUNT DETAILS → REQUIRED FOR CHEQUE
    account_details = serializers.PrimaryKeyRelatedField(
        queryset=AccountDetail.objects.all(),
        required=False,
        allow_null=True,
        help_text="Select bank account (required for Cheque)"
    )

    # NEW: Cheque date
    cheque_date = serializers.DateField(required=False, allow_null=True)
    
    approvals = VoucherApprovalSerializer(many=True, read_only=True)
    required_approvers = serializers.SerializerMethodField()
    approved_count = serializers.SerializerMethodField()
    rejected_count = serializers.SerializerMethodField()

    class Meta:
        model = Voucher
        fields = [
            'id', 'voucher_number', 'voucher_date', 'payment_type', 'name_title', 'pay_to',
            'cheque_number', 'cheque_attachment',
            'cheque_date', 'account_details',
            'attachment', 'created_by', 'created_at', 'status',
            'particulars', 'approvals', 'required_approvers',
            'approved_count', 'rejected_count'
        ]
        read_only_fields = [
            'voucher_number', 'created_by', 'created_at', 'status', 'approvals'
        ]

    def get_required_approvers(self, obj):
        return obj.required_approvers

    def get_approved_count(self, obj):
        return obj.approvals.filter(status='APPROVED').count()

    def get_rejected_count(self, obj):
        return obj.approvals.filter(status='REJECTED').count()

    def validate(self, data):
        if 'particulars' not in data or not data['particulars']:
            raise serializers.ValidationError({'particulars': 'At least one particular is required.'})

        for p in data['particulars']:
            if not p.get('description') or p.get('amount') is None:
                raise serializers.ValidationError({
                    'particulars': 'Each particular must have description and amount.'
                })
            if not p.get('attachment'):
                raise serializers.ValidationError({
                    'particulars': 'Attachment is required for each particular.'
                })

        # === CHEQUE VALIDATION ===
        if data.get('payment_type') == 'CHEQUE':
            cheque_num = data.get('cheque_number', '').strip()
            if not cheque_num:
                raise serializers.ValidationError({'cheque_number': 'Cheque number is required for Cheque payments.'})
            if not cheque_num.isdigit():
                raise serializers.ValidationError({'cheque_number': 'Cheque number must contain only digits.'})
            data['cheque_number'] = cheque_num

            cheque_file = data.get('cheque_attachment')
            if not cheque_file:
                raise serializers.ValidationError({'cheque_attachment': 'Cheque attachment is required for Cheque payments.'})
            if cheque_file.size > 5 * 1024 * 1024:
                raise serializers.ValidationError({'cheque_attachment': 'Cheque attachment size cannot exceed 5 MB.'})

            if not data.get('cheque_date'):
                raise serializers.ValidationError({'cheque_date': 'Cheque date is required for Cheque payments.'})

            # ACCOUNT DETAILS REQUIRED FOR CHEQUE
            if not data.get('account_details'):
                raise serializers.ValidationError({'account_details': 'Account Details is required for Cheque payments.'})

        else:
            data['cheque_number'] = None
            data['cheque_attachment'] = None
            data['cheque_date'] = None
            data['account_details'] = None

        return data

    def validate_attachment(self, value):
        if value and value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("File size cannot exceed 5 MB.")
        return value  # Allow None

    def create(self, validated_data):
        particulars_data = validated_data.pop('particulars', [])
        attachment = validated_data.pop('attachment', None)  # Optional
        cheque_attachment = validated_data.pop('cheque_attachment', None)
        account_details = validated_data.pop('account_details', None)
        
        created_by = validated_data.pop('created_by', None)
        if created_by:
            validated_data['created_by'] = created_by

        voucher = Voucher.objects.create(
            attachment=attachment,  # Can be None
            cheque_attachment=cheque_attachment,
            account_details=account_details,
            **validated_data
        )

        for p_data in particulars_data:
            p_attachment = p_data.pop('attachment')
            Particular.objects.create(voucher=voucher, attachment=p_attachment, **p_data)

        return voucher


# === FIXED: COMPANY DETAIL SERIALIZER (SUPER ADMIN ONLY) ===
class CompanyDetailSerializer(serializers.ModelSerializer):
    logo = serializers.ImageField(
        required=False,
        allow_null=True,
        validators=[
            FileExtensionValidator(allowed_extensions=['png', 'jpg', 'jpeg'])
        ]
    )

    class Meta:
        model = CompanyDetail
        fields = ['id', 'name', 'gst_no', 'pan_no', 'address', 'email', 'phone', 'logo']  # ← ADDED email, phone
        read_only_fields = ['id']

    def validate_logo(self, value):
        if value and value.size > 2 * 1024 * 1024:  # 2 MB
            raise serializers.ValidationError("Logo size cannot exceed 2 MB.")
        return value

    # ← CRITICAL: Return full URL for logo in print
    def to_representation(self, instance):
        ret = super().to_representation(instance)
        request = self.context.get('request')
        if instance.logo:
            if request:
                ret['logo'] = request.build_absolute_uri(instance.logo.url)
            else:
                ret['logo'] = instance.logo.url
        return ret
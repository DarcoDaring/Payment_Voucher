# vouchers/serializers.py
from rest_framework import serializers
from .models import Voucher, Particular, VoucherApproval, ActiveApprovalDesignation
from django.contrib.auth.models import User
from django.core.validators import FileExtensionValidator
from django.db.models import Count, Case, When, IntegerField
from decimal import Decimal, InvalidOperation


class ParticularSerializer(serializers.ModelSerializer):
    attachment = serializers.FileField(
        required=False,
        allow_null=True,
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
        if value and value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("File size cannot exceed 5 MB.")
        return value


class VoucherApprovalSerializer(serializers.ModelSerializer):
    approver = serializers.ReadOnlyField(source='approver.username')
    approved_at = serializers.DateTimeField(format="%d %b %H:%M", read_only=True)

    class Meta:
        model = VoucherApproval
        fields = ['approver', 'status', 'approved_at']


class VoucherSerializer(serializers.ModelSerializer):
    created_by = serializers.ReadOnlyField(source='created_by.username')
    particulars = ParticularSerializer(many=True, required=True)
    
    attachment = serializers.FileField(
        required=True,
        allow_null=False,
        validators=[
            FileExtensionValidator(
                allowed_extensions=['pdf', 'jpg', 'jpeg', 'png', 'doc', 'docx']
            )
        ]
    )

    # NEW: CHEQUE NUMBER
    cheque_number = serializers.CharField(
        max_length=20,
        required=False,
        allow_blank=True,
        allow_null=True
    )
    
    approvals = VoucherApprovalSerializer(many=True, read_only=True)
    required_approvers = serializers.SerializerMethodField()
    approved_count = serializers.SerializerMethodField()
    rejected_count = serializers.SerializerMethodField()

    class Meta:
        model = Voucher
        fields = [
            'id', 'voucher_number', 'voucher_date', 'payment_type', 'name_title', 'pay_to',
            'cheque_number',  # ← ADDED
            'attachment', 'created_by', 'created_at', 'status',
            'particulars', 'approvals', 'required_approvers',
            'approved_count', 'rejected_count'
        ]
        read_only_fields = [
            'voucher_number', 'created_by', 'created_at', 'status', 'approvals'
        ]

    def get_required_approvers(self, obj):
        active_des_ids = ActiveApprovalDesignation.objects.filter(
            is_active=True
        ).values_list('designation__id', flat=True)

        return list(
            User.objects.filter(
                groups__name='Admin Staff',
                userprofile__designation__id__in=active_des_ids
            )
            .values_list('username', flat=True)
            .distinct()
        )

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

        if not data.get('attachment'):
            raise serializers.ValidationError({'attachment': 'This field is required.'})

        # === CHEQUE NUMBER VALIDATION ===
        if data.get('payment_type') == 'CHEQUE':
            cheque_num = data.get('cheque_number', '').strip()
            if not cheque_num:
                raise serializers.ValidationError({'cheque_number': 'Cheque number is required for Cheque payments.'})
            if not cheque_num.isdigit():
                raise serializers.ValidationError({'cheque_number': 'Cheque number must contain only digits.'})
            data['cheque_number'] = cheque_num
        else:
            data['cheque_number'] = None

        return data

    def validate_attachment(self, value):
        if not value:
            raise serializers.ValidationError("Attachment is required.")
        max_size = 5 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError("File size cannot exceed 5 MB.")
        return value

    def create(self, validated_data):
        particulars_data = validated_data.pop('particulars', [])
        attachment = validated_data.pop('attachment', None)
        
        voucher = Voucher.objects.create(
            attachment=attachment,
            created_by=validated_data['created_by'],
            **{k: v for k, v in validated_data.items() if k != 'created_by'}
        )

        for p_data in particulars_data:
            p_attachment = p_data.pop('attachment', None)
            Particular.objects.create(voucher=voucher, attachment=p_attachment, **p_data)

        return voucher
# serializers.py
from rest_framework import serializers
from .models import (
    Voucher, Particular, VoucherApproval, ApprovalLevel,
    AccountDetail, CompanyDetail, MainAttachment,
    ChequeAttachment, ParticularAttachment
)
from django.contrib.auth.models import User
from django.core.validators import FileExtensionValidator
from decimal import Decimal, InvalidOperation


# ============================
# PARTICULAR + ATTACHMENTS
# ============================

class ParticularAttachmentSerializer(serializers.ModelSerializer):
    file = serializers.FileField()

    class Meta:
        model = ParticularAttachment
        fields = ['id', 'file', 'uploaded_at']
        read_only_fields = ['uploaded_at']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        request = self.context.get('request')
        if instance.file:
            if request:
                ret['file'] = request.build_absolute_uri(instance.file.url)
            else:
                ret['file'] = instance.file.url
        return ret


class ParticularSerializer(serializers.ModelSerializer):
    attachments = ParticularAttachmentSerializer(many=True, read_only=True)
    
    # This field accepts new files during create/update
    attachment_files = serializers.ListField(
        child=serializers.FileField(
            max_length=None,
            allow_empty_file=False,
            validators=[
                FileExtensionValidator(
                    allowed_extensions=['pdf', 'jpg', 'jpeg', 'png', 'doc', 'docx']
                )
            ]
        ),
        write_only=True,
        required=False,
        allow_empty=False,
        help_text="Upload one or more attachment files for this particular"
    )

    class Meta:
        model = Particular
        fields = ['id', 'description', 'amount', 'attachments', 'attachment_files']

    def validate_amount(self, value):
        if isinstance(value, str):
            value = value.replace(',', '').strip()
        try:
            value = Decimal(value)
        except (InvalidOperation, ValueError, TypeError):
            raise serializers.ValidationError("Invalid amount format.")
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value

    def validate(self, data):
        # Ensure at least one attachment (existing or new)
        new_files = data.get('attachment_files', [])
        instance = getattr(self, 'instance', None)

        has_existing = False
        if instance and hasattr(instance, 'attachments'):
            has_existing = instance.attachments.exists()

        if not has_existing and not new_files:
            raise serializers.ValidationError({
                "attachment_files": "At least one attachment is required for each particular."
            })

        return data


# ============================
# MAIN & CHEQUE ATTACHMENTS
# ============================

class MainAttachmentSerializer(serializers.ModelSerializer):
    file = serializers.FileField()

    class Meta:
        model = MainAttachment
        fields = ['id', 'file', 'uploaded_at']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        request = self.context.get('request')
        if instance.file and request:
            ret['file'] = request.build_absolute_uri(instance.file.url)
        return ret


class ChequeAttachmentSerializer(serializers.ModelSerializer):
    file = serializers.FileField()

    class Meta:
        model = ChequeAttachment
        fields = ['id', 'file', 'uploaded_at']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        request = self.context.get('request')
        if instance.file and request:
            ret['file'] = request.build_absolute_uri(instance.file.url)
        return ret


# ============================
# VOUCHER APPROVAL
# ============================

class VoucherApprovalSerializer(serializers.ModelSerializer):
    approver = serializers.ReadOnlyField(source='approver.username')
    approved_at = serializers.DateTimeField(format="%d %b %H:%M", read_only=True)
    rejection_reason = serializers.CharField(allow_blank=True, allow_null=True, required=False)

    class Meta:
        model = VoucherApproval
        fields = ['approver', 'status', 'approved_at', 'rejection_reason']


# ============================
# ACCOUNT DETAIL (for dropdown)
# ============================

class AccountDetailSerializer(serializers.ModelSerializer):
    value = serializers.IntegerField(source='id')
    label = serializers.CharField(source='__str__')

    class Meta:
        model = AccountDetail
        fields = ['value', 'label']
# ============================
# MAIN VOUCHER SERIALIZER
# ============================

class VoucherSerializer(serializers.ModelSerializer):
    created_by = serializers.ReadOnlyField(source='created_by.username')
    particulars = ParticularSerializer(many=True)
    main_attachments = MainAttachmentSerializer(many=True, read_only=True)
    cheque_attachments = ChequeAttachmentSerializer(many=True, read_only=True)

    approvals = VoucherApprovalSerializer(many=True, read_only=True)
    required_approvers = serializers.SerializerMethodField()
    approved_count = serializers.SerializerMethodField()
    rejected_count = serializers.SerializerMethodField()

    class Meta:
        model = Voucher
        fields = [
            'id', 'voucher_number', 'voucher_date', 'payment_type',
            'name_title', 'pay_to', 'cheque_number', 'cheque_date',
            'account_details', 'created_by', 'created_at', 'status',
            'main_attachments', 'cheque_attachments',
            'particulars', 'approvals', 'required_approvers',
            'approved_count', 'rejected_count'
        ]
        read_only_fields = [
            'voucher_number', 'created_by', 'created_at', 'status',
            'main_attachments', 'cheque_attachments', 'approvals'
        ]

    # ----------------------------
    # EXTRA FIELDS
    # ----------------------------
    def get_required_approvers(self, obj):
        return obj.required_approvers

    def get_approved_count(self, obj):
        return obj.approvals.filter(status='APPROVED').count()

    def get_rejected_count(self, obj):
        return obj.approvals.filter(status='REJECTED').count()

    # ----------------------------
    # VALIDATION
    # ----------------------------
    def validate(self, data):
        payment_type = data.get('payment_type')

        if payment_type == 'CHEQUE':
            if not data.get('cheque_number'):
                raise serializers.ValidationError({'cheque_number': 'Required for Cheque payments.'})
            if not data.get('cheque_date'):
                raise serializers.ValidationError({'cheque_date': 'Required for Cheque payments.'})
            if not data.get('account_details'):
                raise serializers.ValidationError({'account_details': 'Required for Cheque payments.'})
        else:
            data['cheque_number'] = None
            data['cheque_date'] = None
            data['account_details'] = None

        return data

    # ----------------------------
    # CREATE
    # ----------------------------
    def create(self, validated_data):
        particulars_data = validated_data.pop('particulars', [])
        voucher = Voucher.objects.create(**validated_data)

        # Save particulars + attachments
        for p_data in particulars_data:
            files = p_data.pop('attachment_files', [])
            particular = Particular.objects.create(voucher=voucher, **p_data)
            for f in files:
                ParticularAttachment.objects.create(particular=particular, file=f)

        return voucher

    # ----------------------------
    # UPDATE
    # ----------------------------
    def update(self, instance, validated_data):

        # ---- Update basic fields ----
        particulars_data = validated_data.pop('particulars', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # ============================
        # UPDATE PARTICULARS + FILES
        # ============================
        if particulars_data is not None:

            # delete old attachment files
            for p in instance.particulars.all():
                for att in p.attachments.all():
                    if att.file:
                        att.file.delete(save=False)
                p.attachments.all().delete()

            # delete old particulars
            instance.particulars.all().delete()

            # create new ones
            for p_data in particulars_data:
                files = p_data.pop('attachment_files', [])
                particular = Particular.objects.create(voucher=instance, **p_data)

                for f in files:
                    ParticularAttachment.objects.create(
                        particular=particular,
                        file=f
                    )

        # ============================
        # UPDATE MAIN ATTACHMENTS
        # ============================
        request = self.context.get("request")

        if request:
            new_main_files = request.FILES.getlist("main_attachments")

            if new_main_files:
                # DELETE OLD FILES
                for att in instance.main_attachments.all():
                    if att.file:
                        att.file.delete(save=False)
                instance.main_attachments.all().delete()

                # CREATE NEW FILES
                for file in new_main_files:
                    MainAttachment.objects.create(
                        voucher=instance,
                        file=file
                    )

        return instance

# ============================
# COMPANY DETAIL SERIALIZER
# ============================

class CompanyDetailSerializer(serializers.ModelSerializer):
    logo = serializers.ImageField(
        required=False,
        allow_null=True,
        validators=[FileExtensionValidator(allowed_extensions=['png', 'jpg', 'jpeg'])]
    )

    class Meta:
        model = CompanyDetail
        fields = ['id', 'name', 'gst_no', 'pan_no', 'address', 'email', 'phone', 'logo']
        read_only_fields = ['id']

    def validate_logo(self, value):
        if value and value.size > 2 * 1024 * 1024:
            raise serializers.ValidationError("Logo cannot exceed 2 MB.")
        return value

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        request = self.context.get('request')

        if instance.logo:
            if request:
                ret['logo'] = request.build_absolute_uri(instance.logo.url)
            else:
                ret['logo'] = instance.logo.url

        return ret

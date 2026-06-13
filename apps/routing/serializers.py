from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        min_length=2, max_length=255,
        help_text="Starting US location (e.g. 'New York, NY')"
    )
    end = serializers.CharField(
        min_length=2, max_length=255,
        help_text="Destination US location (e.g. 'Los Angeles, CA')"
    )

    def validate_start(self, value):
        return value.strip()

    def validate_end(self, value):
        return value.strip()

    def validate(self, attrs):
        if attrs["start"].lower() == attrs["end"].lower():
            raise serializers.ValidationError(
                {"end": "Start and end locations must be different."}
            )
        return attrs


@api_view(["POST"])
@permission_classes([AllowAny])
def auth_refresh(request):
    """
    Exchange a refresh token for new access/id tokens.
    Expects { "refresh_token": "..." }
    """
    token = request.data.get("refresh_token")
    if not token:
        return Response({"error": "missing refresh_token"}, status=400)

    try:
        from .auth_utils import refresh_with_cognito
        new_tokens = refresh_with_cognito(token)
        # normalize response
        return Response({
            "access_token": new_tokens.get("access_token"),
            "id_token": new_tokens.get("id_token"),
            "expires_in": new_tokens.get("expires_in"),
            "token_type": new_tokens.get("token_type", "Bearer"),
        })
    except Exception as e:
        return Response({"error": str(e)}, status=400)

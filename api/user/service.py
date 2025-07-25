"""
User logic/code.
"""

from typing import Optional
from sqlalchemy import exists
from sqlalchemy.future import select
from fastapi import APIRouter, Header, Request, HTTPException, Security, status
from bittensor_wallet.keypair import Keypair
from api.config import settings
from api.metasync import MetagraphNode
from api.database import get_session
from api.user.schemas import User
from api.api_key.util import get_and_check_api_key
from api.user.tokens import get_user_from_token
from fastapi.security import APIKeyHeader
from api.constants import HOTKEY_HEADER, SIGNATURE_HEADER, AUTHORIZATION_HEADER
from api.constants import NONCE_HEADER
from api.util import nonce_is_valid, get_signing_message
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


def get_current_user(
    purpose: str = None,
    registered_to: int = None,
    raise_not_found: bool = True,
    allow_api_key=False,
):
    """
    Authentication dependency builder.
    """

    async def _authenticate(
        request: Request,
        api_key: Optional[str] = Security(api_key_header),
        hotkey: str | None = Header(None, alias=HOTKEY_HEADER),
        signature: str | None = Header(None, alias=SIGNATURE_HEADER),
        nonce: str | None = Header(None, alias=NONCE_HEADER),
        authorization: str | None = Header(None, alias=AUTHORIZATION_HEADER),
    ):
        """
        Helper to authenticate requests.
        """

        use_hotkey_auth = hotkey and signature
        # If not using hotkey auth, then just use the API key
        if not use_hotkey_auth:
            # API key validation.
            if authorization:
                token = authorization.split(" ")[-1]

                # JWT auth.
                if (
                    token
                    and authorization.lower().lstrip().startswith("bearer ")
                    and not token.strip().startswith("cpk_")
                ):
                    return await get_user_from_token(token, request)

                # API key auth.
                if token:
                    api_key = await get_and_check_api_key(token, request)
                    request.state.api_key = api_key
                    return api_key.user

            # NOTE: Need a nicer error message if the user is trying to register (and has no api key)
            if raise_not_found:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Can't find a user with that api key in our db :(",
                )
            return None

        # Otherwise we are using hotkey auth, so need to check the nonce
        # and check the message was signed correctly
        if not nonce_is_valid(nonce):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid nonce!",
            )

        # Custom whitelist from potentially compromised hotkey.
        # Request message:
        #   btcli w sign --message "whitelist IP 207.246.94.14"
        #   4063dc072f57f6ce77ad1208dc002373c7c491c5f3d75248a51b856b061f6838656a9052b0717bbed20e437cd6e168c784605cd9adb8fb2f90b9a1b25e94528a
        #   My cold key is 5C5zpdLSSxFeFkLFw9tAc7DdxdK82GCAjnoe5pub73GMvKLt
        # miner hotkey 5FhMaRd59y5nyDEtCz1JMMEMZzAGimtmC8m5AfCeXVE3vzCx
        if purpose not in ("sockets", "registry"):
            origin_ip = request.headers.get("x-forwarded-for", "").split(",")[0]
            client_ip = request.client.host
            if hotkey == "5FhMaRd59y5nyDEtCz1JMMEMZzAGimtmC8m5AfCeXVE3vzCx":
                if origin_ip != "207.246.94.14" and client_ip != "207.246.94.14":
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail=f"Unauthorized IP address: {origin_ip=} {client_ip}",
                    )

        # Now get the Signing message
        body_sha256 = getattr(request.state, "body_sha256", None)

        signing_message = get_signing_message(
            hotkey=hotkey,
            nonce=nonce,
            payload_hash=body_sha256,
            purpose=purpose,
            payload_str=None,
        )

        if not signing_message:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Bad signing message: {signing_message}",
            )

        # Verify the signature
        try:
            signature_hex = bytes.fromhex(signature)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid signature: {signature}, with error: {e}",
            )
        try:
            keypair = Keypair(hotkey)
            if not keypair.verify(signing_message, signature_hex):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid request signature for hotkey {hotkey}. Message: {signing_message}",
                )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid request signature for hotkey {hotkey}. Message: {signing_message}",
            ) from e

        # Requires a hotkey registered to a netuid?
        if registered_to is not None:
            async with get_session() as session:
                if not (
                    await session.execute(
                        select(
                            exists()
                            .where(MetagraphNode.hotkey == hotkey)
                            .where(MetagraphNode.netuid == registered_to)
                        )
                    )
                ).scalar():
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail=f"Hotkey is not registered on netuid {settings.netuid}",
                    )

        # Fetch the actual user.
        # NOTE: We should have a standard way to get this session
        async with get_session() as session:
            session: AsyncSession  # For nice type hinting for IDE's
            result = await session.execute(select(User).where(User.hotkey == hotkey))

            user = result.scalar_one_or_none()
            if not user and raise_not_found and not registered_to:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Could not find user with hotkey: {hotkey}",
                )
            return user

    return _authenticate


async def chutes_user_id():
    if (user_id := getattr(router, "_chutes_user_id", None)) is not None:
        return user_id
    async with get_session() as session:
        router._chutes_user_id = (
            (await session.execute(select(User.user_id).where(User.username == "chutes")))
            .unique()
            .scalar_one_or_none()
        )
    return router._chutes_user_id


async def chutes_user():
    if (user := getattr(router, "_chutes_user", None)) is not None:
        return user
    async with get_session() as session:
        router._chutes_user = (
            (await session.execute(select(User).where(User.username == "chutes")))
            .unique()
            .scalar_one_or_none()
        )
    return router._chutes_user

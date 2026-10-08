from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr


class UserResponse(BaseModel):
    # from_attributes: собирается из AuthenticatedUser, который отдаёт сервис.
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    role: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse

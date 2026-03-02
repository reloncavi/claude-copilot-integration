# =============================================================================
# app/core/repository.py — Repository Pattern: Clases Base
# Stack: SQLAlchemy 2.0 async · PostgreSQL · FastAPI Depends()
# Patrón: Generic Repository + Unit of Work
# Objetivo: Toda operación DB exclusivamente aquí, NUNCA en routers/services
# =============================================================================

from abc import ABC, abstractmethod
from typing import TypeVar, Generic, Optional, Sequence, AsyncGenerator, Any
from uuid import UUID, uuid4
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
    AsyncEngine,
)
from sqlalchemy import select, func, and_, asc, desc
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import logging
import os

logger = logging.getLogger(__name__)


# =============================================================================
# MODELOS BASE SQLAlchemy
# =============================================================================

class Base(DeclarativeBase):
    """Base declarativa para todos los modelos SQLAlchemy del proyecto."""
    pass


class BaseModel(Base):
    """
    Modelo base abstracto con campos comunes para todas las entidades.
    Todos los modelos del proyecto deben heredar de aquí.
    """
    __abstract__ = True

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def to_dict(self) -> dict:
        """
        Serializa el modelo a dict.
        Convierte UUID y datetime a string para compatibilidad JSON.
        Excluye atributos internos de SQLAlchemy.
        """
        result = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, UUID):
                value = str(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            result[column.name] = value
        return result

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id}>"


# =============================================================================
# TYPE VARS PARA GENERICS
# =============================================================================

ModelType = TypeVar('ModelType', bound=BaseModel)
CreateSchemaType = TypeVar('CreateSchemaType')  # Pydantic schema para crear
UpdateSchemaType = TypeVar('UpdateSchemaType')  # Pydantic schema para actualizar


# =============================================================================
# DATABASE MANAGER
# Maneja engine, session factory y lifecycle de conexiones
# =============================================================================

class DatabaseManager:
    """
    Gestor central de conexiones a PostgreSQL.
    Configurado para async con pool de conexiones robusto.

    Uso:
        db = DatabaseManager(os.environ["DATABASE_URL"])
        async with db.session() as session:
            result = await session.execute(select(User))
    """

    def __init__(self, database_url: str):
        # Validar que sea una URL async (asyncpg)
        if database_url.startswith('postgresql://'):
            database_url = database_url.replace(
                'postgresql://', 'postgresql+asyncpg://', 1
            )

        self._engine: AsyncEngine = create_async_engine(
            database_url,
            echo=os.environ.get('SQLALCHEMY_ECHO', 'false').lower() == 'true',
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,          # verifica conexiones antes de usarlas
            pool_recycle=3600,           # reciclar conexiones cada 1h
        )

        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,      # evita lazy loading después del commit
            autoflush=False,
            autocommit=False,
        )

        logger.info(f"DatabaseManager inicializado")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Context manager que provee una sesión con rollback automático en error.

        Uso:
            async with db.session() as session:
                result = await session.execute(...)
        """
        async with self._session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Context manager con commit automático al salir sin errores.
        Ideal para Unit of Work: múltiples repos comparten una transacción.

        Uso:
            async with db.transaction() as session:
                user = await user_repo.create(session, data)
                await order_repo.create(session, order_data)
                # commit automático aquí
        """
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    async def create_tables(self) -> None:
        """Crea todas las tablas definidas en los modelos. Usar en tests o dev."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Tablas creadas correctamente")

    async def drop_tables(self) -> None:
        """Elimina todas las tablas. SOLO para tests."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        logger.warning("Todas las tablas eliminadas")

    async def dispose(self) -> None:
        """Cierra el pool de conexiones. Llamar en el shutdown de la app."""
        await self._engine.dispose()
        logger.info("DatabaseManager: pool de conexiones cerrado")


# =============================================================================
# INTERFAZ ABSTRACTA DEL REPOSITORY
# Contrato que todos los repositories deben cumplir
# =============================================================================

class AbstractRepository(ABC, Generic[ModelType]):
    """
    Interfaz base para todos los repositories del proyecto.
    Define el contrato CRUD mínimo obligatorio.
    """

    @abstractmethod
    async def get_by_id(self, id: UUID) -> Optional[ModelType]:
        """Busca una entidad por su primary key. Retorna None si no existe."""
        ...

    @abstractmethod
    async def get_all(self, skip: int = 0, limit: int = 100) -> Sequence[ModelType]:
        """Lista entidades con paginación offset/limit."""
        ...

    @abstractmethod
    async def create(self, obj_in: Any) -> ModelType:
        """Crea una nueva entidad desde un Pydantic schema."""
        ...

    @abstractmethod
    async def update(self, id: UUID, obj_in: Any) -> Optional[ModelType]:
        """Actualiza una entidad. Retorna None si no existe."""
        ...

    @abstractmethod
    async def delete(self, id: UUID) -> bool:
        """Elimina una entidad. Retorna True si existía, False si no."""
        ...

    @abstractmethod
    async def count(self) -> int:
        """Cuenta el total de registros."""
        ...

    @abstractmethod
    async def exists(self, id: UUID) -> bool:
        """Verifica si existe una entidad sin cargar el objeto completo."""
        ...


# =============================================================================
# IMPLEMENTACIÓN CONCRETA SQLAlchemy ASYNC
# Repository genérico que implementa todas las operaciones CRUD
# =============================================================================

class SQLAlchemyRepository(AbstractRepository[ModelType], Generic[ModelType]):
    """
    Implementación genérica del repository usando SQLAlchemy 2.0 async.

    Uso:
        class UserRepository(SQLAlchemyRepository[UserModel]):
            def __init__(self, session: AsyncSession):
                super().__init__(session, UserModel)

            async def get_by_email(self, email: str) -> Optional[UserModel]:
                stmt = select(UserModel).where(UserModel.email == email)
                result = await self.session.execute(stmt)
                return result.scalar_one_or_none()
    """

    def __init__(self, session: AsyncSession, model_class: type[ModelType]):
        """
        Args:
            session: AsyncSession inyectada (no crear aquí — viene de FastAPI Depends)
            model_class: clase del modelo SQLAlchemy (ej: UserModel)
        """
        self.session = session
        self.model_class = model_class

    async def get_by_id(self, id: UUID) -> Optional[ModelType]:
        """Busca por primary key usando SQLAlchemy 2.0 select()."""
        stmt = select(self.model_class).where(self.model_class.id == id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all(self, skip: int = 0, limit: int = 100) -> Sequence[ModelType]:
        """
        Lista con paginación. Orden: created_at DESC (más recientes primero).
        Max limit: 1000 para prevenir queries masivas accidentales.
        """
        limit = min(limit, 1000)
        stmt = (
            select(self.model_class)
            .order_by(desc(self.model_class.created_at))
            .offset(skip)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_field(
        self,
        field_name: str,
        value: Any,
    ) -> Sequence[ModelType]:
        """
        Búsqueda genérica por cualquier campo del modelo.

        Args:
            field_name: nombre del atributo (ej: 'email', 'status')
            value: valor a buscar
        """
        column = getattr(self.model_class, field_name, None)
        if column is None:
            raise AttributeError(
                f"El modelo {self.model_class.__name__} "
                f"no tiene el campo '{field_name}'"
            )
        stmt = select(self.model_class).where(column == value)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, obj_in: Any) -> ModelType:
        """
        Crea una nueva entidad desde un Pydantic schema.
        Usa flush() en lugar de commit() para respetar el Unit of Work.
        """
        data = obj_in.model_dump() if hasattr(obj_in, 'model_dump') else dict(obj_in)
        db_obj = self.model_class(**data)
        self.session.add(db_obj)
        await self.session.flush()       # genera el ID sin commit
        await self.session.refresh(db_obj)  # carga campos default de la DB
        logger.debug(f"Creado {self.model_class.__name__} id={db_obj.id}")
        return db_obj

    async def create_many(self, objects: list[Any]) -> Sequence[ModelType]:
        """
        Bulk insert eficiente para múltiples entidades.
        Usa add_all() + flush() para mantener el Unit of Work.
        """
        db_objects = []
        for obj_in in objects:
            data = obj_in.model_dump() if hasattr(obj_in, 'model_dump') else dict(obj_in)
            db_objects.append(self.model_class(**data))

        self.session.add_all(db_objects)
        await self.session.flush()

        # Refresh para cargar IDs y defaults generados por la DB
        for db_obj in db_objects:
            await self.session.refresh(db_obj)

        logger.debug(
            f"Bulk insert: {len(db_objects)} {self.model_class.__name__}"
        )
        return db_objects

    async def update(self, id: UUID, obj_in: Any) -> Optional[ModelType]:
        """
        Actualización parcial (solo campos enviados en el schema).
        Usa exclude_unset=True para no sobreescribir campos no enviados.
        """
        db_obj = await self.get_by_id(id)
        if db_obj is None:
            return None

        update_data = (
            obj_in.model_dump(exclude_unset=True)
            if hasattr(obj_in, 'model_dump')
            else dict(obj_in)
        )

        for key, value in update_data.items():
            setattr(db_obj, key, value)

        # Actualizar timestamp manualmente (por si onupdate no funciona en flush)
        db_obj.updated_at = datetime.now(timezone.utc)

        await self.session.flush()
        await self.session.refresh(db_obj)
        logger.debug(f"Actualizado {self.model_class.__name__} id={id}")
        return db_obj

    async def delete(self, id: UUID) -> bool:
        """
        Elimina una entidad por ID.
        Returns False si no existía (no lanza excepción).
        """
        db_obj = await self.get_by_id(id)
        if db_obj is None:
            return False

        await self.session.delete(db_obj)
        await self.session.flush()
        logger.debug(f"Eliminado {self.model_class.__name__} id={id}")
        return True

    async def count(self) -> int:
        """Cuenta todos los registros de la tabla."""
        stmt = select(func.count()).select_from(self.model_class)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def exists(self, id: UUID) -> bool:
        """
        Verifica existencia sin cargar el objeto.
        Más eficiente que get_by_id() cuando solo necesitas saber si existe.
        """
        stmt = (
            select(func.count())
            .select_from(self.model_class)
            .where(self.model_class.id == id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one() > 0


# =============================================================================
# FILTER MIXIN
# Para repositories que necesiten búsqueda dinámica con múltiples filtros
# =============================================================================

class FilterMixin(Generic[ModelType]):
    """
    Mixin que agrega capacidad de filtrado dinámico a cualquier repository.

    Uso:
        class UserRepository(SQLAlchemyRepository[UserModel], FilterMixin[UserModel]):
            ...

        # En el service:
        users, total = await repo.find_with_filters(
            filters={"status": "active", "role": ["admin", "manager"]},
            order_by="created_at",
            order_dir="desc",
            skip=0,
            limit=20,
        )
    """

    async def find_with_filters(
        self,
        filters: dict[str, Any],
        order_by: str = 'created_at',
        order_dir: str = 'desc',
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[Sequence[ModelType], int]:
        """
        Búsqueda con filtros dinámicos y paginación.

        Tipos de filtro soportados:
        - Valor simple:  {"status": "active"}  →  WHERE status = 'active'
        - Lista:         {"role": ["admin", "manager"]}  →  WHERE role IN (...)
        - ILIKE:         {"name": "%john%"}  →  WHERE name ILIKE '%john%'

        Returns:
            (items, total_count) — útil para respuestas paginadas con metadata
        """
        conditions = []
        for field_name, value in filters.items():
            column = getattr(self.model_class, field_name, None)
            if column is None:
                logger.warning(
                    f"Campo de filtro ignorado: '{field_name}' "
                    f"no existe en {self.model_class.__name__}"
                )
                continue

            if isinstance(value, list):
                conditions.append(column.in_(value))
            elif isinstance(value, str) and '%' in value:
                conditions.append(column.ilike(value))
            else:
                conditions.append(column == value)

        # Construir orden
        order_column = getattr(self.model_class, order_by, None)
        if order_column is None:
            order_column = self.model_class.created_at
            logger.warning(
                f"Campo de orden '{order_by}' no encontrado. "
                f"Usando created_at."
            )
        order_expr = desc(order_column) if order_dir == 'desc' else asc(order_column)

        where_clause = and_(*conditions) if conditions else True

        # Query paginada
        stmt = (
            select(self.model_class)
            .where(where_clause)
            .order_by(order_expr)
            .offset(skip)
            .limit(min(limit, 1000))
        )

        # Count total (sin paginación) para metadata de respuesta
        count_stmt = (
            select(func.count())
            .select_from(self.model_class)
            .where(where_clause)
        )

        items_result = await self.session.execute(stmt)
        count_result = await self.session.execute(count_stmt)

        return items_result.scalars().all(), count_result.scalar_one()


# =============================================================================
# UNIT OF WORK
# Coordina múltiples repositories en una sola transacción atómica
# =============================================================================

class UnitOfWork:
    """
    Implementa el patrón Unit of Work para operaciones multi-repo.
    Garantiza atomicidad: todos los cambios se confirman o revierten juntos.

    Uso en service:
        async with UnitOfWork(db_manager) as uow:
            user = await uow.users.create(user_data)
            order = await uow.orders.create(order_data)
            # commit automático al salir del bloque
            # rollback automático si hay excepción

    Para control manual:
        async with UnitOfWork(db_manager) as uow:
            user = await uow.users.create(user_data)
            await uow.commit()   # commit parcial
            # continuar con más operaciones...
    """

    def __init__(self, db_manager: DatabaseManager):
        self._db_manager = db_manager
        self.session: Optional[AsyncSession] = None

        # Repositories disponibles — agregar según módulos del proyecto
        # Se inicializan en __aenter__ con la MISMA sesión para atomicidad
        self.users: Optional[SQLAlchemyRepository] = None
        self.orders: Optional[SQLAlchemyRepository] = None
        # Extender aquí: self.products, self.payments, etc.

    async def __aenter__(self) -> 'UnitOfWork':
        self.session = self._db_manager._session_factory()

        # Inicializar todos los repositories con la misma sesión
        # Descomentar y agregar los modelos reales del proyecto:
        # from app.modules.users.models import UserModel
        # from app.modules.orders.models import OrderModel
        # self.users  = SQLAlchemyRepository(self.session, UserModel)
        # self.orders = SQLAlchemyRepository(self.session, OrderModel)

        return self

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: Any,
    ) -> None:
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
                logger.error(
                    f"UnitOfWork rollback por excepción: "
                    f"{exc_type.__name__}: {exc_val}"
                )
        finally:
            await self.session.close()
            self.session = None

    async def commit(self) -> None:
        """Commit explícito para control manual dentro del bloque with."""
        if self.session:
            await self.session.commit()

    async def rollback(self) -> None:
        """Rollback explícito para control manual dentro del bloque with."""
        if self.session:
            await self.session.rollback()


# =============================================================================
# DEPENDENCY INJECTION (FastAPI)
# Factories para inyectar en routers vía Depends()
# =============================================================================

# Instancia global del DatabaseManager
# Inicializar en el startup de la app:
#   app.state.db = DatabaseManager(os.environ["DATABASE_URL"])
_db_manager: Optional[DatabaseManager] = None


def init_db(database_url: str) -> DatabaseManager:
    """
    Inicializa el DatabaseManager global.
    Llamar en el startup de la aplicación FastAPI.

    En main.py:
        @app.on_event("startup")
        async def startup():
            init_db(os.environ["DATABASE_URL"])
    """
    global _db_manager
    _db_manager = DatabaseManager(database_url)
    return _db_manager


def get_db_manager() -> DatabaseManager:
    """Retorna el DatabaseManager global. Raises si no fue inicializado."""
    if _db_manager is None:
        raise RuntimeError(
            "DatabaseManager no inicializado. "
            "Llama a init_db() en el startup de la app."
        )
    return _db_manager


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency de FastAPI que provee una sesión por request.

    En el router:
        @router.get("/users")
        async def list_users(session: AsyncSession = Depends(get_db_session)):
            repo = UserRepository(session)
            return await repo.get_all()
    """
    async with get_db_manager().session() as session:
        yield session


async def get_unit_of_work() -> AsyncGenerator[UnitOfWork, None]:
    """
    Dependency de FastAPI que provee un UnitOfWork por request.
    Commit/rollback automático al finalizar el request.

    En el router:
        @router.post("/orders")
        async def create_order(
            data: OrderCreate,
            uow: UnitOfWork = Depends(get_unit_of_work)
        ):
            return await order_service.create(uow, data)
    """
    async with UnitOfWork(get_db_manager()) as uow:
        yield uow


# =============================================================================
# EJEMPLO: REPOSITORY CONCRETO
# Patrón de referencia para crear repositories específicos por módulo
# Copiar este patrón en app/modules/<modulo>/repository.py
# =============================================================================

# from app.modules.users.models import UserModel
#
# class UserRepository(SQLAlchemyRepository[UserModel], FilterMixin[UserModel]):
#     """
#     Repository del módulo de usuarios.
#     Hereda CRUD genérico + capacidad de filtrado dinámico.
#     """
#
#     def __init__(self, session: AsyncSession):
#         super().__init__(session, UserModel)
#
#     async def get_by_email(self, email: str) -> Optional[UserModel]:
#         """Búsqueda por email (unique). Usado en autenticación."""
#         stmt = select(UserModel).where(UserModel.email == email)
#         result = await self.session.execute(stmt)
#         return result.scalar_one_or_none()
#
#     async def get_active_users(self, skip: int = 0, limit: int = 100) -> Sequence[UserModel]:
#         """Lista solo usuarios activos, ordenados por nombre."""
#         stmt = (
#             select(UserModel)
#             .where(UserModel.is_active == True)
#             .order_by(asc(UserModel.created_at))
#             .offset(skip)
#             .limit(limit)
#         )
#         result = await self.session.execute(stmt)
#         return result.scalars().all()
#
#     async def deactivate(self, id: UUID) -> Optional[UserModel]:
#         """Soft delete: marca is_active=False en lugar de eliminar."""
#         db_obj = await self.get_by_id(id)
#         if db_obj is None:
#             return None
#         db_obj.is_active = False
#         db_obj.updated_at = datetime.now(timezone.utc)
#         await self.session.flush()
#         await self.session.refresh(db_obj)
#         return db_obj

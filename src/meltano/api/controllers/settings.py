from flask import Blueprint, jsonify, request
from flask_restful import Api, Resource, fields, marshal, marshal_with
from flask_security import roles_required
from flask_principal import Permission, Need
from werkzeug.exceptions import Forbidden
from sqlalchemy.orm import joinedload
from meltano.api.security import api_auth_required, users
from meltano.api.models.security import db, User, Role, RolesUsers, RolePermissions
from .settings_helper import SettingsHelper
from .project_helper import project_from_slug, project_api_route

settingsBP = Blueprint("settings", __name__, url_prefix=project_api_route("settings"))
settingsApi = Api(settingsBP)


@settingsBP.before_request
@api_auth_required
def before_request():
    pass


@settingsBP.route("/", methods=["GET"])
@project_from_slug
# @roles_required("admin")
def index(project):
    settings_helper = SettingsHelper(project)
    return jsonify(settings_helper.get_connections())


@settingsBP.route("/save", methods=["POST"])
@project_from_slug
@roles_required("admin")
def save(project):
    settings_helper = SettingsHelper(project)

    connection = request.get_json()
    settings = settings_helper.save_connection(connection)
    return jsonify(settings)


@settingsBP.route("/delete", methods=["POST"])
@project_from_slug
def delete(project):
    settings_helper = SettingsHelper(project)
    connection = request.get_json()
    settings = settings_helper.delete_connection(connection)
    return jsonify(settings)


@settingsBP.route("/connections/<name>/test")
@project_from_slug
def test(name, project):
    current_settings = Settings.query.first().settings
    connections = current_settings["connections"]
    try:
        found_connection = next(
            connection for connection in connections if connection["name"] == name
        )
    # this is a really broad exception catch, this will swallow sneaky errors
    except Exception as e:
        found_connection = {}

    return jsonify(found_connection)


class Canonical(fields.Raw):
    def __init__(self, scopes=[], **kwargs):
        super().__init__(**kwargs)

        self._scopes = scopes

    def format(self, value):
        try:
            return value.canonical(self._scopes)
        except AttributeError:
            return value.id


class AclResource(Resource):
    UserDefinition = {
        "username": fields.String,
        "active": fields.Boolean,
        "confirmed_at": fields.DateTime,
        "roles": fields.List(Canonical),
    }

    RoleDefinition = {
        "name": fields.String,
        "description": fields.String,
        "permissions": fields.List(Canonical(scopes=[Role])),
    }

    PermissionDefinition = {"type": fields.String, "name": fields.String}

    AclDefinition = {
        "users": fields.List(fields.Nested(UserDefinition)),
        "roles": fields.List(fields.Nested(RoleDefinition)),
        "permissions": fields.List(fields.Nested(PermissionDefinition)),
    }

    @marshal_with(AclDefinition)
    @project_from_slug
    @roles_required("admin")
    def get(self, project):
        return {
            "users": User.query.options(joinedload("roles")).all(),
            "roles": Role.query.options(joinedload("permissions")).all(),
            "permissions": [],
        }


class RolesResource(Resource):
    @marshal_with(AclResource.RoleDefinition)
    @project_from_slug
    @roles_required("admin")
    def post(self, project):
        payload = request.get_json()
        role = payload["role"]
        user = payload.get("user")
        try:
            role_name = role.pop("name")
            role = users.find_or_create_role(role_name, **role)

            if user:
                user = users.get_user(user)
                users.add_role_to_user(user, role_name)
        except KeyError:
            return "role.name must be set.", 400

        db.session.commit()
        return role, 201

    @roles_required("admin")
    @project_from_slug
    @marshal_with(AclResource.RoleDefinition)
    def delete(self, project):
        payload = request.get_json()
        role = payload["role"]
        user = payload.get("user")

        try:
            role = Role.query.filter_by(name=role["name"]).one()

            if not user:
                if role == "admin":
                    return "The `admin` role cannot be deleted.", 403

                # delete the role
                db.session.delete(role)
            else:
                # unassign the user-role
                assigned_user = role.users.filter(User.username == user).one()
                role.users.remove(assigned_user)

            db.session.commit()
        except Exception as err:
            return str(err), 400

        return role, 201


class RolePermissionsResource(Resource):
    def _parse_request(self):
        payload = request.get_json()
        return payload["role"], payload["permissionType"], payload["context"]

    @roles_required("admin")
    @project_from_slug
    @marshal_with(AclResource.RoleDefinition)
    def post(self, project):
        role, permission_type, context = self._parse_request()

        try:
            role = (
                Role.query.options(joinedload("permissions"))
                .filter_by(name=role["name"])
                .one()
            )
            perm = RolePermissions.query.filter_by(
                role=role, type=permission_type, context=context
            ).first()

            if not perm:
                perm = RolePermissions(role=role, type=permission_type, context=context)
                role.permissions.append(perm)

            db.session.commit()
        except Exception as err:
            return str(err), 400

        return role, 200

    @roles_required("admin")
    @project_from_slug
    @marshal_with(AclResource.RoleDefinition)
    def delete(self, project):
        role, permission_type, context = self._parse_request()
        role = Role.query.filter_by(name=role["name"]).one()

        try:
            perm = RolePermissions.query.filter_by(
                role=role, type=permission_type, context=context
            ).one()
            role.permissions.remove(perm)
            db.session.commit()
        except:
            return role, 200

        return role, 201


settingsApi.add_resource(AclResource, "/acl")
settingsApi.add_resource(
    RolePermissionsResource, "/acl/roles/permissions", endpoint="role_permissions"
)
settingsApi.add_resource(RolesResource, "/acl/roles", endpoint="roles")

package oracle;

import static org.junit.Assert.assertSame;

import ch.njol.skript.classes.Changer.ChangeMode;
import ch.njol.skript.Skript;
import ch.njol.skript.expressions.ExprShooter;
import ch.njol.skript.lang.Expression;
import java.lang.reflect.Proxy;
import java.util.concurrent.atomic.AtomicReference;
import org.bukkit.entity.LivingEntity;
import org.bukkit.entity.Projectile;
import org.junit.Test;
import sun.misc.Unsafe;

public class ExprShooterBehaviorOracleTest {
    @Test
    public void settingShooterUpdatesEveryPublicProjectile() throws Exception {
        initializeRegistrationEnvironment();
        AtomicReference<Object> storedShooter = new AtomicReference<>();
        Projectile projectile = (Projectile) Proxy.newProxyInstance(
            getClass().getClassLoader(), new Class<?>[] {Projectile.class}, (proxy, method, args) -> {
                if (method.getName().equals("setShooter")) {
                    storedShooter.set(args[0]);
                    return null;
                }

                if (method.getName().equals("getShooter")) {
                    return storedShooter.get();
                }
                return primitiveDefault(method.getReturnType());
            });
        LivingEntity shooter = (LivingEntity) Proxy.newProxyInstance(
            getClass().getClassLoader(), new Class<?>[] {LivingEntity.class},
            (proxy, method, args) -> primitiveDefault(method.getReturnType()));
        Expression<?> input = (Expression<?>) Proxy.newProxyInstance(
            getClass().getClassLoader(), new Class<?>[] {Expression.class},
            (proxy, method, args) -> method.getName().equals("getArray")
                ? new Projectile[] {projectile} : primitiveDefault(method.getReturnType()));
        ExprShooter expression = new ExprShooter();
        expression.init(new Expression<?>[] {input}, 0, null, null);
        expression.change(null, new Object[] {shooter}, ChangeMode.SET);
        assertSame(shooter, projectile.getShooter());
    }

    private static void initializeRegistrationEnvironment() throws Exception {
        java.lang.reflect.Field unsafeField = Unsafe.class.getDeclaredField("theUnsafe");
        unsafeField.setAccessible(true);
        Skript plugin = (Skript) ((Unsafe) unsafeField.get(null)).allocateInstance(Skript.class);
        java.lang.reflect.Field instance = Skript.class.getDeclaredField("instance");
        instance.setAccessible(true);
        instance.set(null, plugin);
        java.lang.reflect.Field enabled =
            Skript.class.getSuperclass().getDeclaredField("isEnabled");
        enabled.setAccessible(true);
        enabled.setBoolean(plugin, true);
    }

    private static Object primitiveDefault(Class<?> type) {
        if (!type.isPrimitive()) return null;
        if (type == boolean.class) return false;
        if (type == char.class) return '\0';
        if (type == byte.class) return (byte) 0;
        if (type == short.class) return (short) 0;
        if (type == int.class) return 0;
        if (type == long.class) return 0L;
        if (type == float.class) return 0F;
        return 0D;
    }
}

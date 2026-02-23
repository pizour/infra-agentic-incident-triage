import sys
from django.core.exceptions import ObjectDoesNotExist
from dcim.models import Site, Manufacturer, DeviceType, DeviceRole, Device, Interface
from ipam.models import IPAddress

def get_or_create(model, **kwargs):
    obj, created = model.objects.get_or_create(**kwargs)
    return obj

def seed():
    print("=== Seeding NetBox via ORM ===")
    
    # 1. Site
    site = get_or_create(Site, name="Home Lab", slug="home-lab", status="active")
    
    # 2. Manufacturer
    manufacturer = get_or_create(Manufacturer, name="Generic", slug="generic")
    
    # 3. Device Type
    device_type = get_or_create(DeviceType, manufacturer=manufacturer, model="Laptop", slug="laptop")
    
    # 4. Device Role
    role = get_or_create(DeviceRole, name="Server", slug="server", color="0000ff")
    
    # 5. Device
    device, created = Device.objects.get_or_create(
        name="z-laptop",
        defaults={
            "site": site,
            "device_type": device_type,
            "device_role": role,
            "status": "active"
        }
    )
    if not created:
        device.site = site
        device.device_type = device_type
        device.device_role = role
        device.status = "active"
        device.save()
    
    # 6. Interface
    interface = get_or_create(Interface, device=device, name="eth0", type="1000base-t")
    
    # 7. IP Address
    ip, created = IPAddress.objects.get_or_create(address="192.168.100.176/24", defaults={"status": "active"})
    
    # Assign IP to interface
    ip.assigned_object = interface
    ip.save()
    
    # Set primary ip4
    device.primary_ip4 = ip
    device.save()
    
    print("=== Seeding complete! ===")

if __name__ == "__main__":
    seed()

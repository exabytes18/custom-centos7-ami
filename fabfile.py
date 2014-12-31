# http://boto.readthedocs.org/en/latest/ref/ec2.html
import boto.ec2
import re
import time

# http://docs.fabfile.org/en/latest/api/core/operations.html
from fabric.api import abort, env, put, sudo
from fabric.decorators import runs_once, task

env.ec2_key_pair_name = 'exabytes18@geneva'
env.ec2_region = 'us-east-1'
env.user = 'centos'

env.ec2_instances = {
    'buildbox': {
        'ami': 'ami-96a818fe',
        'type': 'r3.large',
        'bid': 0.1,
        'security_groups': ['SSH Only'],
    }
}


def _launch_instance_abort_on_error(ami,
                                    bid,
                                    instance_type,
                                    security_groups):

    bdm = boto.ec2.blockdevicemapping.BlockDeviceMapping()
    bdm['/dev/sdb'] = boto.ec2.blockdevicemapping.BlockDeviceType(
        delete_on_termination=False,
        size=4,
        volume_type='gp2')

    ec2 = boto.ec2.connect_to_region(env.ec2_region)
    sirs = ec2.request_spot_instances(
        price=bid,
        image_id=ami,
        count=1,
        type='one-time',
        key_name=env.ec2_key_pair_name,
        instance_type=instance_type,
        block_device_map=bdm,
        security_groups=security_groups)

    instance_ids = set()
    while True:
        time.sleep(10)
        done = True
        for sir in ec2.get_all_spot_instance_requests(map(lambda x: x.id, sirs)):
            print 'State:  %s' % sir.state
            print 'Fault:  %s' % sir.fault
            print 'Status: %s' % sir.status.message
            if sir.state not in ('open', 'active'):
                abort('Failed to launch instances')
            if sir.state == 'open':
                done = False
            if sir.state == 'active':
                instance_ids.add(sir.instance_id)

        if done:
            break

    print ''
    print 'Instances:'
    for reservation in ec2.get_all_instances(list(instance_ids)):
        for instance in reservation.instances:
            print '    %s' % instance.id
            print '        type:        %s' % instance.instance_type
            print '        internal ip: %s' % instance.private_ip_address
            print '        public ip:   %s' % instance.ip_address


@task
@runs_once
def spot_prices():
    ec2 = boto.ec2.connect_to_region(env.ec2_region)
    pricing = ec2.get_spot_price_history(product_description='Linux/UNIX')

    type_az_sph = {}
    for sph in pricing:
        type_az = type_az_sph.setdefault(sph.instance_type, {})
        if sph.availability_zone not in type_az or \
                sph.availability_zone > type_az[sph.availability_zone].timestamp:
            type_az[sph.availability_zone] = sph

    def _inst_cmp(a, b):
        am = re.match(r'(.+?)(\d*)\.(\d*)(.+)', a[0])
        bm = re.match(r'(.+?)(\d*)\.(\d*)(.+)', b[0])

        a_cat, a_gen = (am.group(1), int(am.group(2)))
        b_cat, b_gen = (bm.group(1), int(bm.group(2)))

        ranks = ['micro', 'small', 'medium', 'large', 'xlarge']
        a_rank = ranks.index(am.group(4))
        b_rank = ranks.index(bm.group(4))
        a_xlarge_rank = int(am.group(3) or 0)
        b_xlarge_rank = int(bm.group(3) or 0)

        if a_cat < b_cat:
            return -1
        elif a_cat > b_cat:
            return 1
        elif a_gen < b_gen:
            return -1
        elif a_gen > b_gen:
            return 1
        elif a_rank < b_rank:
            return -1
        elif a_rank > b_rank:
            return 1
        elif a_xlarge_rank < b_xlarge_rank:
            return -1
        elif a_xlarge_rank > b_xlarge_rank:
            return 1
        else:
            return 0

    last_inst_cls = None
    print ''
    for instance_type, az_sph in sorted(type_az_sph.iteritems(), _inst_cmp):
        inst_cls = instance_type.partition('.')[0]
        if last_inst_cls != inst_cls and last_inst_cls is not None:
            print '    ' + '-' * 25

        print '    %s' % instance_type
        for az, sph in sorted(az_sph.iteritems(), lambda a, b: cmp(a[0], b[0])):
            print '        %s: %f' % (az, sph.price)

        last_inst_cls = inst_cls


@task
@runs_once
def launch(name):
    config = env.ec2_instances[name]
    _launch_instance_abort_on_error(
        ami=config['ami'],
        bid=config['bid'],
        instance_type=config['type'],
        security_groups=config['security_groups'])


@task
@runs_once
def register_image(snapshot):
    ec2 = boto.ec2.connect_to_region(env.ec2_region)
    ec2.register_image(
        name='Centos 7.0',
        description='Centos 7.0',
        architecture='x86_64',
        virtualization_type='hvm',
        root_device_name='/dev/sda1',
        snapshot_id=snapshot,
        delete_root_volume_on_termination=True)


@task
def build_image():
    # prepare the volume
    sudo('! mountpoint -q /mnt/ami || umount /mnt/ami')
    sudo('parted -a optimal /dev/xvdb -s mklabel gpt mkpart primary 2048s 6144s mkpart primary 8192s 100% set 1 bios_grub on')
    sudo('mkfs.xfs -f /dev/xvdb2')
    sudo('mkdir -p /mnt/ami')
    sudo('grep -qi "/dev/xvdb2" /etc/fstab || echo "/dev/xvdb2 /mnt/ami xfs defaults 0 0" >> /etc/fstab')
    sudo('mount /mnt/ami')

    # bind /dev and /proc from buildbox
    sudo('mountpoint -q /mnt/ami/dev || mkdir -p /mnt/ami/dev && mount -o bind /dev /mnt/ami/dev')
    sudo('mountpoint -q /mnt/ami/proc || mkdir -p /mnt/ami/proc && mount -o bind /proc /mnt/ami/proc')

    # install the base packages and any thing else that we want in our ami
    put('files/yum.conf', '/tmp/yum.conf')
    sudo('yum -c /tmp/yum.conf --installroot=/mnt/ami -y groupinstall Base')
    sudo('yum -c /tmp/yum.conf --installroot=/mnt/ami -y install dhclient e2fsprogs selinux-policy selinux-policy-targeted openssh openssh-server openssh-clients vim bzip2 sudo ntp gcc autoconf automake make libtool grub2')
    sudo('yum -c /tmp/yum.conf --installroot=/mnt/ami -y remove plymouth plymouth-core-libs plymouth-scripts')

    # install bootloader
    sudo('''cat <<EOF > /mnt/ami/etc/default/grub
GRUB_TIMEOUT=1
GRUB_DISTRIBUTOR="$(sed 's, release .*$,,g' /etc/system-release)"
GRUB_DEFAULT=saved
GRUB_DISABLE_SUBMENU=true
GRUB_TERMINAL="serial console"
GRUB_SERIAL_COMMAND="serial --speed=115200"
GRUB_CMDLINE_LINUX="console=ttyS0,115200 console=tty0 vconsole.font=latarcyrheb-sun16 crashkernel=auto  vconsole.keymap=us"
GRUB_DISABLE_RECOVERY="true"
EOF''')
    sudo('grub2-install --boot-directory=/mnt/ami/boot /dev/xvdb')
    sudo('chroot /mnt/ami grub2-mkconfig -o /boot/grub2/grub.cfg')

    # setup fstab
    sudo('''UUID=`blkid -o value -s UUID /dev/xvdb2`; cat <<EOF > /mnt/ami/etc/fstab
UUID=$UUID    /               xfs             defaults        1    1
none            /dev/pts        devpts          gid=5,mode=620  0    0
none            /dev/shm        tmpfs           defaults        0    0
none            /proc           proc            defaults        0    0
none            /sys            sysfs           defaults        0    0
EOF''')

    # setup eth0
    sudo('''cat <<EOF > /mnt/ami/etc/sysconfig/network-scripts/ifcfg-eth0
DEVICE="eth0"
BOOTPROTO="dhcp"
ONBOOT="yes"
TYPE="Ethernet"
USERCTL="yes"
PEERDNS="yes"
IPV6INIT="no"
PERSISTENT_DHCLIENT="1"
EOF''')

    # enable networking
    sudo('''cat <<EOF > /mnt/ami/etc/sysconfig/network
NETWORKING=yes
NOZEROCONF=yes
EOF''')

    # no more touching the image after we relabel
    sudo('chroot /mnt/ami /sbin/fixfiles -f -F relabel || true')

    # freeze the volume so we get a clean snapshot
    sudo('sync')
    sudo('udevadm settle')
    sudo('xfs_freeze -f /mnt/ami')

    # snapshot

    # unfreeze
    sudo('xfs_freeze -u /mnt/ami')

    # cleanup
    sudo('umount /mnt/ami/dev')
    sudo('umount /mnt/ami/proc')
